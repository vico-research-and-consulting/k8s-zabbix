FROM python:3.11-alpine
LABEL maintainer="operations@vico-research.com"
LABEL Description="zabbix-kubernetes - efficent kubernetes monitoring for zabbix"

MAINTAINER operations@vico-research.com

ENV K8S_API_HOST ""
ENV K8S_API_TOKEN ""
ENV ZABBIX_SERVER "zabbix"
ENV ZABBIX_HOST "k8s"
ENV CRYPTOGRAPHY_DONT_BUILD_RUST "1"

WORKDIR /app
COPY --chown=nobody:users base                                /app/base/
COPY --chown=nobody:users k8sobjects                          /app/k8sobjects/ 
COPY --chown=nobody:users check_kubernetesd config_default.py /app/
COPY --chown=nobody:users Pipfile Pipfile.lock                /app/

RUN  apk update && apk upgrade --update-cache --available
RUN  apk add bash 
RUN  pip install --root-user-action=ignore --upgrade pip && pip install --root-user-action=ignore pipenv
RUN  PIPENV_USE_SYSTEM=1 pipenv install --system
RUN  rm -rf /var/cache/apk/

USER nobody
ENTRYPOINT [ "/app/check_kubernetesd" ]
