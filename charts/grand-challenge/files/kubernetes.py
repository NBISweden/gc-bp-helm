import logging
from ipaddress import ip_address
import json
from pathlib import Path
from socket import getaddrinfo
from tempfile import TemporaryDirectory

import os
import boto3
import datetime

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
    __s3_client = None

    @staticmethod
    def _s3_endpoint():
        endpoint = os.environ.get("AWS_S3_ENDPOINT_URL")
        if os.environ.get("AWS_S3_INTERNAL_URL"):
            endpoint = os.environ.get("AWS_S3_INTERNAL_URL")

        return endpoint

    @property
    def _s3_client(self):
        if not self.__s3_client:
            endpoint = self._s3_endpoint()
            self.__s3_client = boto3.client("s3", endpoint_url=endpoint)
        return self.__s3_client

    @staticmethod
    def serviceaccount_base():
        return "/var/run/secrets/kubernetes.io/serviceaccount/"

    @classmethod
    def namespace(cls):
        with open(cls.serviceaccount_base() + "/namespace") as f:
            return f.readline()

    @classmethod
    def token(cls):
        with open(cls.serviceaccount_base() + "/token") as f:
            return f.readline()

    @staticmethod
    def kubeserver_url():
        return f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT_HTTPS']}"

    @classmethod
    def cafile(cls):
        return cls.serviceaccount_base() + "/ca.crt"

    @classmethod
    def kubecall(cls, urlpart, method="GET", data=None):
        baseurl = cls.kubeserver_url()
        ca = cls.cafile()
        ns = cls.namespace()
        h = {"Authorization": f"Bearer {cls.token()}", "Accept": "application/json"}
        useurl = f"{baseurl}/{urlpart.replace('_:NS:_', ns).strip('/')}"

        kwa = {}
        if data:
            h["Content-Type"] = "application/json"
            kwa["data"] = data

        logger.debug(f"Calling {useurl} with {method}, passing {h}")
        logger.debug(f"Keyword arguments {kwa}")

        r = requests.request(
            method=method,
            url=useurl,
            verify=ca,
            headers=h,
            **kwa,
        )

        if not r.ok:
            logger.error(f"Call to {useurl} failed, returned {r.text}")
            raise requests.exceptions.RequestException(
                f"Request response was not ok: got {r.status_code}"
            )

        return r

    @classmethod
    def get_jobs(cls):
        return cls.kubecall("/apis/batch/v1/namespaces/_:NS:_/jobs").json()["items"]

    @classmethod
    def get_pods(cls):
        return cls.kubecall("/api/v1/namespaces/_:NS:_/pods").json()["items"]

    @classmethod
    def get_registry_ip(cls):
        return cls.kubecall("/api/v1/namespaces/_:NS:_/services/registry").json()[
            "spec"
        ]["clusterIP"]

    @classmethod
    def create_job(cls, jobspec):
        r = cls.kubecall(
            f"/apis/batch/v1/namespaces/_:NS:_/jobs/", method="POST", data=jobspec
        )

    @classmethod
    def delete_job(cls, name):
        r = cls.kubecall(
            f"/apis/batch/v1/namespaces/_:NS:_/jobs/{name}",
            method="DELETE",
        )

    @classmethod
    def kill_pod(cls, job_name):
        pod = cls.get_job_pod(job_name)

        if not pod or "metadata" not in pod:
            return

        name = pod["metadata"]["name"]
        r = cls.kubecall(
            f"/api/v1/namespaces/_:NS:_/pods/{name}",
            method="DELETE",
        )

    @classmethod
    def get_logs(cls, name, tailLines=-1):
        return cls.kubecall(
            f"/api/v1/namespaces/_:NS:_/pods/{name}/log?timestamps{'&'+str(tailLines) if tailLines >0 else ''}"
        ).text

    @property
    def job_name(self):
        return self._job_id

    def execute(self, *, input_civs, input_prefixes):
        self.submit_job(input_civs=input_civs, input_prefixes=input_prefixes)

    def handle_event(self, *, event):
        raise RuntimeError("This backend is not event-driven")

    def deprovision(self):
        super().deprovision()

        jobs = self.get_jobs()
        for j in jobs:
            if "metadata" in j and j["metadata"]["name"] == self.job_name:
                self.delete_job(name=self.job_name)
        self.kill_pod(self.job_name)

    @staticmethod
    def get_job_params(*, event):
        raise NotImplementedError

    @property
    def duration(self):
        try:
            details = self.get_job_pod(self.job_name)

            if not details:
                return

            if "metadata" in details and "creationTimestamp" in details["metadata"]:
                md = details["metadata"]
                logger.debug(f"Pod details: {details}")

                started_at = isoparse(md["creationTimestamp"]).timestamp()

                finished_at = datetime.datetime.utcnow().timestamp()

                if "deletionTimestamp" in md:
                    finished_at = isoparse(md["deletionTimestamp"]).timestamp()

                return datetime.timedelta(seconds=finished_at - started_at)

        except requests.exceptions.RequestException:
            return None
        return None

    @classmethod
    def get_job_pod(cls, job_name):
        try:
            details = cls.get_pods()

            for pod in details:
                if not "metadata" in pod:
                    continue
                if not "labels" in pod["metadata"]:
                    continue
                if not "job-name" in pod["metadata"]["labels"]:
                    continue

                if pod["metadata"]["labels"]["job-name"] == job_name:
                    return pod

        except requests.exceptions.RequestException:
            return None
        return None

    @property
    def container_ip(self):
        pod = self.get_job_pod(self.job_name)

        if pod and "podIP" in pod["status"]:
            return pod["status"]["podIP"]

        return None

    @property
    def container_logs(self):
        return self.get_logs(self.get_job_pod(self.job_name)["metadata"]["name"])

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
                        "value": self._s3_endpoint(),
                    },
                ]
            )

        jobspec = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": f"{self.job_name}"},
            "spec": {
                "template": {
                    "metadata": {
                        "labels":
                          {"algorithm" : "job"}
                    }
                    "spec": {
                        "containers": [
                            {
                                "name": f"{self.job_name}-runner",
                                "image": self._exec_image_repo_tag.replace(
                                    "registry:5000", f"{self.get_registry_ip()}:5000"
                                ),
                                "args": ["serve"],
                                "env": environment,
                            }
                        ],
                        "restartPolicy": "Never",
                    }
                },
                "backoffLimit": 4,
            },
        }

        logger.debug(f"Jobspec is:\n {jobspec}")
        try:
            self.create_job(json.dumps(jobspec))
            self._await_container_ready()
            try:
                response = requests.post(
                    f"http://{self.container_ip}:8080/invocations",
                    json=self._get_invocation_json(
                        input_civs=input_civs, input_prefixes=input_prefixes
                    ),
                    timeout=self._time_limit,
                )
            except requests.exceptions.Timeout:
                raise ComponentException("Time limit exceeded")
        finally:
            self._set_task_logs()
            self.delete_job(name=self.job_name)

        logger.debug(f"Response from {response.text}")
        r = response.json()
        exit_code = int(r["return_code"])

        if exit_code > 128:
            raise ComponentException(
                f"The container was killed by signal {exit_code-128}"
            )
        elif exit_code != 0:
            raise ComponentException(user_error(self.stderr))

    def _await_container_ready(self):
        logger.debug("Await container ready")
        attempts = 0
        while True:
            attempts += 1
            logger.debug(f"Await container ready, attempt {attempts}")

            try:
                # Timeout is from the SageMaker API definitions
                # https://docs.aws.amazon.com/sagemaker/latest/dg/your-algorithms-batch-code.html#your-algorithms-batch-algo-ping-requests
                response = requests.get(
                    f"http://{self.container_ip}:8080/ping",
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
            loglines = self.container_logs
        except ObjectDoesNotExist:
            return

        print(loglines)
        self._stderr = []
        # Expects a list of lines, so we should give it that
        self._stdout = loglines.split("\n")
