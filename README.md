# Grand-challenge helm charts

This is a first approximation of grand-challenge helm charts, building
on the docker setup provided byt the GC project.


## Base repository

This builds on the Grand-challenge repo <https://github.com/comic/grand-challenge.org>

## Image refresh

A `docker pull` of the original image should present a line like
`Digest: sha256:...` you can just pick the digest from there.

## Development setup

### Setup Minikube
```Bash
minikube start 
minikube addons enable ingress
minikube addons enable ingress-dns
```

### Set up Cluster issuer
```Bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.1/cert-manager.yaml
```


## User setup

The base reposiotry contains a script directory with scripts that can be
modified so they are suitable and brought over (`kubectl cp`) and executed on
platform with `./manage.py runscript SCRIPTNAME`

In particular, there's a "create_superuser.py" script that can assist in
creating a user in a new setup (`./manage.py runscript create_superuser`).

## Required configurations

Most of `values.yaml` needs to be adapted.

The `publicIP` should be a CIDR notation that contains any of the IPs of the
Ingress. In particular, this means whatever name you use for `domainName` should
resolve to an IP within the `publicIP` network.

`internalAPIIP` should be a CIDR that contains the API service IP as shown by

```
kubectl get -n default service kubernetes -o jsonpath --template='
  {"\n"}{.spec.clusterIP}{"\n\n"}'
```

`internalAPIEndpoints` should be a CIDR-notation network containing the IPs
reported by

```
kubectl get -n default endpoints kubernetes -o jsonpath --template='
   {"\n"}{.subsets[].addresses}{"\n\n"}'
```

`internalAPIPort` should be what you see for

```
kubectl get -n default endpoints kubernetes -o jsonpath --template='
  {"\n"}{.subsets[].ports[].port}{"\n\n"}
```

Other options are hopefully self documentary