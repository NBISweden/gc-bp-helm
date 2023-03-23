import logging
from ipaddress import ip_address
from json import JSONDecodeError
from pathlib import Path
from socket import getaddrinfo
from tempfile import TemporaryDirectory

import requests
from dateutil.parser import isoparse
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from grandchallenge.components.backends.base import Executor
from grandchallenge.components.backends.exceptions import ComponentException
from grandchallenge.components.backends.utils import (
    LOGLINES,
    SourceChoices,
    user_error,
)
from grandchallenge.components.tasks import _repo_login_and_run

logger = logging.getLogger(__name__)


class KubernetesExecutor(Executor):
    @staticmethod
    def serviceaccount_base():
        return "/var/run/secrets/kubernetes.io/serviceaccount/"

    @staticmethod
    def namespace():
        with open(serviceaccount_base() + "/namespace") as f:
            return f.readline()

    @staticmethod
    def token():
        with open(serviceaccount_base() + "/token") as f:
            return f.readline()

    @staticmethod
    def kubeserver_url():
        return f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT_HTTPS']}/"

    @staticmethod
    def cafile():
        return serviceaccount_base() + "/ca.crt"

    @staticmethod
    def kubecall(urlpart, method="GET", data=None):
        baseurl = kubeserver_url()
        ca = cafile()
        ns = namespace()
        h = {"Authorization", f"Bearer {token()}"}

        kwa = {}
        if data:
            kwa["data"] = data

        r = requests.request(
            method=method,
            url=f"{baseurl}/{urlpart.replace('_:NS:_', ns)}",
            verify=ca,
            headers=h,
            **kwa,
        )

        if not r.ok:
            raise requests.exceptions.RequestException(
                f"Request response was not ok: got {r.status_code}"
            )

        return r

    @staticmethod
    def get_jobs():
        return kubecall("/batch/apis/v1/namespaces/_:NS:_/jobs").json()["items"]

    @staticmethod
    def get_pods():
        return kubecall("/api/v1/namespaces/_:NS:_/pods").json()["items"]

    def create_job(self, jobspec):
        r = kubecall(
            "/batch/apis/v1/namespaces/_:NS:_/jobs/{self.container_name}", method="POST"
        )

    def delete_job(self, name):
        r = kubecall(
            "/batch/apis/v1/namespaces/_:NS:_/jobs/{self.container_name}",
            method="DELETE",
        )

    def get_logs(self, name, tailLines=-1):
        return kubecall(
            "/api/v1/namespaces/_:NS:_/pods/{name}/log?timestamps{'&'+str(tailLines) if tailLines >0 else ''}"
        ).text

    @property
    def container_name(self):
        return self._job_id

    def execute(self, *, input_civs, input_prefixes):
        self.submit_job(input_civs=input_civs, input_prefixes=input_prefixes)

    def handle_event(self, *, event):
        raise RuntimeError("This backend is not event-driven")

    def deprovision(self):
        super().deprovision()
        delete_job(name=self.container_name)

    @staticmethod
    def get_job_params(*, event):
        raise NotImplementedError

    @property
    def duration(self):
        try:
            details = get_jobs()

            for p in details:
                if p["metadata"]["name"] == self.container_name:
                    s = p["status"]

                    if s["succeeded"] > 0:
                        started_at = s["startTime"]
                        finished_at = s["completionTime"]
                        return isoparse(finished_at) - isoparse(started_at)

        except requests.exceptions.RequestException:
            return None
        return None

    @property
    def container_ip(self):
        try:
            details = get_pods()

            for pod in details:
                if (
                    pod["metadata"]["ownerReferences"][0]["name"] == self.container_name
                    and pod["metadata"]["ownerReferences"][0]["kind"] == "Job"
                ):
                    if "podIP" in pod["status"]:
                        return pod["status"]["podIP"]

        except requests.exceptions.RequestException:
            return None
        return None

    @property
    def container_logs(self):
        try:
            details = get_pods()

            for pod in details:
                if (
                    pod["metadata"]["ownerReferences"][0]["name"] == self.container_name
                    and pod["metadata"]["ownerReferences"][0]["kind"] == "Job"
                ):
                    return get_logs(pod["metadata"]["name"])

        except requests.exceptions.RequestException:
            return None
        return None

    @property
    def cents_per_hour(self):
        return 100

    @property
    def runtime_metrics(self):
        logger.warning("Runtime metrics are not implemented for this backend")
        return

    def submit_job(self, *, input_civs, input_prefixes) -> None:
        environment = [
            {
                "name": "NVIDIA_VISIBLE_DEVICES",
                "value": settings.COMPONENTS_NVIDIA_VISIBLE_DEVICES,
            }
        ]

        if settings.COMPONENTS_DOCKER_TASK_SET_AWS_ENV:
            environment.extend(
                [
                    {
                        "name": "AWS_ACCESS_KEY_ID",
                        "value": settings.COMPONENTS_DOCKER_TASK_AWS_ACCESS_KEY_ID,
                    },
                    {
                        "name": "AWS_SECRET_ACCESS_KEY",
                        "value": settings.COMPONENTS_DOCKER_TASK_AWS_SECRET_ACCESS_KEY,
                    },
                    {
                        "name": "AWS_S3_ENDPOINT_URL",
                        "value": settings.AWS_S3_ENDPOINT_URL,
                    },
                ]
            )

        jobspec = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": {self.container_name}},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": f"{self.container_name}-runner",
                                "image": self._exec_image_repo_tag,
                                "args": ["serve"],
                                "env": environment,
                            }
                        ],
                        "restartPolicy": "Never",
                    },
                    "backoffLimit": 4,
                }
            },
        }

        try:
            self.create_job(jobspec)
            self._await_container_ready()
            try:

                response = requests.post(
                    f"http://{container_ip}:8080/invocations",
                    json=self._get_invocation_json(
                        input_civs=input_civs, input_prefixes=input_prefixes
                    ),
                    timeout=self._time_limit,
                )
            except requests.exceptions.Timeout:
                raise ComponentException("Time limit exceeded")
        finally:
            self._set_task_logs()
            delete_job(name=self.container_name)

        response = response.json()
        exit_code = int(response["return_code"])

        if exit_code > 128:
            raise ComponentException(
                f"The container was killed by signal {exit_code-128}"
            )
        elif exit_code != 0:
            raise ComponentException(user_error(self.stderr))

    def _await_container_ready(self):
        attempts = 0
        while True:
            attempts += 1

            try:
                # Timeout is from the SageMaker API definitions
                # https://docs.aws.amazon.com/sagemaker/latest/dg/your-algorithms-batch-code.html#your-algorithms-batch-algo-ping-requests
                response = requests.get(
                    f"http://{container_ip}:8080/ping",
                    timeout=2,
                )
            except requests.exceptions.RequestException:
                continue

            if response.status_code == 200:
                break
            elif attempts > 10:
                raise ComponentException("Container did not start in time")

    def _set_task_logs(self):
        try:
            loglines = container_logs
        except ObjectDoesNotExist:
            return

        self._stderr = []
        self._stdout = loglines
