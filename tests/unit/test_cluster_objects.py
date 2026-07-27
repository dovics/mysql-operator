import copy
import unittest
from unittest.mock import MagicMock, patch

from kubernetes import client

from mysqloperator.controller.innodbcluster import cluster_objects


class SubsystemObjectUpdateTest(unittest.TestCase):
    def setUp(self):
        self.subsystem = "metrics"
        self.logger = MagicMock()
        self.sts = client.V1StatefulSet(
            metadata=client.V1ObjectMeta(
                name="test-cluster",
                namespace="test-ns",
            ),
            spec=client.V1StatefulSetSpec(
                selector=client.V1LabelSelector(match_labels={"app": "mysql"}),
                service_name="test-cluster-instances",
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(),
                    spec=client.V1PodSpec(containers=[
                        client.V1Container(name="mysql"),
                    ]),
                ),
            ),
        )
        self.service = client.V1Service(
            metadata=client.V1ObjectMeta(
                name="test-cluster-instances",
                namespace="test-ns",
            ),
            spec=client.V1ServiceSpec(
                ports=[client.V1ServicePort(name="mysql", port=3306)],
            ),
        )
        self.cluster = MagicMock()
        self.cluster.get_stateful_set.return_value = self.sts
        self.cluster.get_service.return_value = self.service
        self.cluster.parsed_spec.get_configmaps_cbs = {}
        self.cluster.parsed_spec.get_secrets_cbs = {}
        self.cluster.parsed_spec.get_svc_monitor_cbs = {}

    def make_patcher(self):
        patcher = MagicMock()
        patcher.sts = MagicMock()
        patcher.sts.spec = cluster_objects.spec_to_dict(
            copy.deepcopy(self.sts.spec))
        patcher.server_sts_patch = {}
        patcher.sts_changed = False
        patcher.sts_template_changed = False
        patcher.sts_spec_changed = False
        return patcher

    def test_noop_sts_callback_does_not_leave_patch_or_schedule_restart(self):
        def set_existing_service_name(sts, patcher, logger):
            patcher.server_sts_patch = {
                "spec": {"serviceName": "test-cluster-instances"}
            }
            patcher.sts_changed = True

        self.cluster.parsed_spec.add_to_sts_cbs = {
            self.subsystem: [set_existing_service_name]
        }
        self.cluster.parsed_spec.get_add_to_svc_cbs = {}
        patcher = self.make_patcher()

        cluster_objects.update_objects_for_subsystem(
            self.subsystem, self.cluster, patcher, self.logger)

        patcher.patch_sts.assert_not_called()
        self.assertEqual(patcher.server_sts_patch, {})
        self.assertFalse(patcher.sts_changed)

    def test_changed_sts_callback_schedules_restart(self):
        def change_service_name(sts, patcher, logger):
            patcher.server_sts_patch = {
                "spec": {"serviceName": "replacement-service"}
            }
            patcher.sts_changed = True

        self.cluster.parsed_spec.add_to_sts_cbs = {
            self.subsystem: [change_service_name]
        }
        self.cluster.parsed_spec.get_add_to_svc_cbs = {}
        patcher = self.make_patcher()

        cluster_objects.update_objects_for_subsystem(
            self.subsystem, self.cluster, patcher, self.logger)

        patcher.patch_sts.assert_called_once()
        restart_patch = patcher.patch_sts.call_args.args[0]
        self.assertIn(
            "kubectl.kubernetes.io/restartedAt",
            restart_patch["spec"]["template"]["metadata"]["annotations"])

    def test_noop_service_callback_does_not_replace_service(self):
        self.cluster.parsed_spec.add_to_sts_cbs = {}
        self.cluster.parsed_spec.get_add_to_svc_cbs = {
            self.subsystem: [MagicMock()]
        }
        patcher = self.make_patcher()

        with patch.object(
                cluster_objects.api_core,
                "replace_namespaced_service") as replace_service:
            cluster_objects.update_objects_for_subsystem(
                self.subsystem, self.cluster, patcher, self.logger)

        replace_service.assert_not_called()

    def test_changed_service_is_replaced(self):
        def add_metrics_port(service, logger):
            service.spec.ports.append(
                client.V1ServicePort(name="metrics", port=9104))

        self.cluster.parsed_spec.add_to_sts_cbs = {}
        self.cluster.parsed_spec.get_add_to_svc_cbs = {
            self.subsystem: [add_metrics_port]
        }
        patcher = self.make_patcher()

        with patch.object(
                cluster_objects.api_core,
                "replace_namespaced_service") as replace_service:
            cluster_objects.update_objects_for_subsystem(
                self.subsystem, self.cluster, patcher, self.logger)

        replace_service.assert_called_once_with(
            "test-cluster-instances", "test-ns", self.service)
