import logging
import os

import kubernetes
import voluptuous as vol
from azure.mgmt.containerservice.container_service_client import ContainerServiceClient
from azure.mgmt.containerservice.models import CredentialResults

from takeoff.application_version import ApplicationVersion
from takeoff.azure.credentials.active_directory_user import ActiveDirectoryUserCredentials
from takeoff.azure.credentials.keyvault import KeyVaultClient
from takeoff.azure.credentials.subscription_id import SubscriptionId
from takeoff.schemas import TAKEOFF_BASE_SCHEMA
from takeoff.step import Step
from takeoff.util import run_bash_command

logger = logging.getLogger(__name__)

SCHEMA = TAKEOFF_BASE_SCHEMA.extend(
    {
        vol.Required("task"): "k8sImageRollingUpdate",
        # TODO This is a hack to target a specific resource group. This logic needs an overhaul soon.
        vol.Required("resource_group"): str,
        vol.Required("cluster_name"): str,
        vol.Required("deployment_name"): str,
        vol.Required("image"): str,
        vol.Optional("namespace", default="default"): str,
        vol.Optional("always_deploy", default=False): bool,
        "azure": {
            vol.Required(
                "kubernetes_naming",
                description=(
                    "Naming convention for the resource."
                    "This should include the {env} parameter. For example"
                    "aks_{env}"
                ),
            ): str
        },
    },
    extra=vol.ALLOW_EXTRA,
)


# assumes kubectl is available
class K8sImageRollingUpdate(Step):
    def __init__(self, env: ApplicationVersion, config: dict):
        super().__init__(env, config)
        # have to overwrite the default keyvault b/c of Vnet K8s cluster
        self.vault_name, self.vault_client = KeyVaultClient.vault_and_client(self.config, env=env)

    def schema(self) -> vol.Schema:
        return SCHEMA

    def run(self):
        """
        For now only update the deployment image once a tag is created
        """
        if self.config["always_deploy"] or self.env.on_release_tag:
            logger.info("Deploying new k8s image")
            self.update_image()

    def update_image(self):
        # get the ip address for this environment
        self._authenticate_with_k8s()
        # load the kubeconfig we just fetched
        kubernetes.config.load_kube_config()
        logger.info("Kubeconfig loaded")

        self._apply_rolling_update(self.config["namespace"], self.config["deployment_name"])

    def _apply_rolling_update(self, namespace, deployment):
        """
        https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#updating-a-deployment
        """
        new_image = f"{self.config['image']}:{self.env.artifact_tag}"
        logger.info(f"Deploying image {new_image}")

        cmd = [
            "kubectl",
            "--namespace",
            namespace,
            "--record",
            f"deployment.apps/{deployment}",
            "set",
            "image",
            f"deployment.v1.apps/{deployment}",
            f"{deployment}={new_image}",
        ]
        return_code = run_bash_command(cmd)

        if return_code != 0:
            raise ChildProcessError("Could not update the image for some reason!")

    @staticmethod
    def _write_kube_config(credential_results: CredentialResults):
        kubeconfig = credential_results.kubeconfigs[0].value.decode(encoding="UTF-8")

        kubeconfig_dir = os.path.expanduser("~/.kube")

        # assumption here that there is no existing kubeconfig (which makes sense, given this script should
        # be run in a docker container ;-) )
        os.mkdir(kubeconfig_dir)
        with open(kubeconfig_dir + "/config", "w") as f:
            f.write(kubeconfig)

        logger.info("Kubeconfig successfully written")

    def _authenticate_with_k8s(self):
        # get azure container service client
        credentials = ActiveDirectoryUserCredentials(
            vault_name=self.vault_name, vault_client=self.vault_client
        ).credentials(self.config)

        client = ContainerServiceClient(
            credentials=credentials,
            subscription_id=SubscriptionId(self.vault_name, self.vault_client).subscription_id(self.config),
        )

        # authenticate with k8s
        credential_results = client.managed_clusters.list_cluster_user_credentials(
            resource_group_name=self.config["resource_group"], resource_name=self.config["cluster_name"]
        )

        self._write_kube_config(credential_results)