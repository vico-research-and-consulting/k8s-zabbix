import json
import logging
import re

from pyzabbix import ZabbixMetric

from .k8sobject import K8sObject

logger = logging.getLogger(__name__)


class Pod(K8sObject):
    object_type = "pod"
    kind = None

    @property
    def base_name(self):
        # self.kind und self.podname
        if "metadata" not in self.data and "name" in self.data["metadata"]:
            raise Exception(f"Could not find name in metadata for resource {self.resource}")

        if "owner_references" in self.data["metadata"] and isinstance(self.data["metadata"]["owner_references"], dict):
            for owner_refs in self.data["metadata"]["owner_references"]:
                if "kind" in owner_refs:
                    self.kind = owner_refs["kind"]
                    break

        # generate_name = self.data['metadata']['generate_name']
        generate_name = self.data["spec"]["containers"][0]["name"]

        match self.kind:
            case "Job":
                name = re.sub(r"-\d+-$", "", generate_name)
            case "ReplicaSet":
                name = re.sub(r"-[a-f0-9]{4,}-$", "", generate_name)
            case _:
                name = re.sub(r"-$", "", generate_name)

        self.podname = name

        for container in self.data["spec"]["containers"]:
            if container["name"] in self.name:
                return container["name"]
        return self.name

    @property
    def containers(self):
        containers = {}
        for container in self.data["spec"]["containers"]:
            containers.setdefault(container["name"], 0)
            containers[container["name"]] += 1
        return containers

    @property
    def resource_data(self):
        data = super().resource_data
        data["containers"] = json.dumps(self.containers)
        container_status = dict()
        data["ready"] = True
        pod_data = {
            "restart_count": 0,
            "ready": 0,
            "not_ready": 0,
            "status": "OK",
        }
        self.phase = self.data["status"]["phase"]
        logger.error("STATUS_ALL: %s\n" % (self.data["status"]))

        if "container_statuses" in self.data["status"] and self.data["status"]["container_statuses"]:
            for container in self.data["status"]["container_statuses"]:
                status_values = []
                container_name = container["name"]

                # this pod data
                if container_name not in container_status:
                    container_status[container_name] = {
                        "restart_count": 0,
                        "ready": 0,
                        "not_ready": 0,
                        "status": "OK",
                    }
                container_status[container_name]["restart_count"] += container["restart_count"]
                pod_data["restart_count"] += container["restart_count"]

                if container["ready"] is True:
                    container_status[container_name]["ready"] += 1
                    pod_data["ready"] += 1
                elif self.phase not in ["Succeeded", "Running", "Pending"]:
                    container_status[container_name]["not_ready"] += 1
                    pod_data["not_ready"] += 1

                if container["state"] and len(container["state"]) > 0:
                    for status, container_data in container["state"].items():
                        try:
                            terminated_state = container["state"]["terminated"]["reason"]
                        except (KeyError, TypeError):
                            terminated_state = ""
                        # There are three possible container states: Waiting, Running, and Terminated.
                        # not status in ["waiting", "running"]
                        if container_data and status == "terminated" and terminated_state != "Completed":
                            status_values.append(status)

                if len(status_values) > 0:
                    logger.debug("STATUS_ERR: %s\n%s\n" % (status_values, container))
                    container_status[container_name]["status"] = "ERROR: " + (",".join(status_values))
                    pod_data["status"] = container_status[container_name]["status"]
                    data["ready"] = False

        data["container_status"] = json.dumps(container_status)
        data["pod_data"] = json.dumps(pod_data)
        return data

    def get_zabbix_discovery_data(self):
        data = list()
        for container in self.containers:
            data += [
                {
                    "{#NAMESPACE}": self.name_space,
                    "{#NAME}": self.base_name,
                    "{#CONTAINER}": container,
                }
            ]
        return data

    def get_discovery_for_zabbix(self, discovery_data=None):
        if discovery_data is None:
            discovery_data = self.get_zabbix_discovery_data()

        return ZabbixMetric(
            self.zabbix_host,
            "check_kubernetesd[discover,containers]",
            json.dumps(
                {
                    "data": discovery_data,
                }
            ),
        )

    # -> not used, aggregate over containers
    # def get_zabbix_metrics(self):
    #     data = self.resource_data
    #     data_to_send = list()
    #
    #     if 'status' not in data:
    #         logger.error(data)
    #
    #     for k, v in pod_data.items():
    #         data_to_send.append(ZabbixMetric(
    #             self.zabbix_host, 'check_kubernetesd[get,pods,%s,%s,%s]' % (self.name_space, self.name, k),
    #             v,
    #         ))
    #
    #     return data_to_send
