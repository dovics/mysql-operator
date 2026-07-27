import unittest
from unittest.mock import MagicMock, patch

from kubernetes.client.rest import ApiException

from mysqloperator.controller.innodbcluster import operator_cluster


class RecreateMissingStatefulSetTest(unittest.TestCase):
    def test_recreates_from_current_spec(self):
        cluster = MagicMock()
        cluster.namespace = "test-ns"
        cluster.name = "test-cluster"
        cluster.parsed_spec = MagicMock()
        logger = MagicMock()
        statefulset = {"metadata": {"name": cluster.name}}

        with patch.object(
                operator_cluster.cluster_objects,
                "prepare_cluster_stateful_set",
                return_value=statefulset) as prepare, \
             patch.object(operator_cluster.kopf, "adopt") as adopt, \
             patch.object(operator_cluster.api_apps,
                          "create_namespaced_stateful_set") as create:
            operator_cluster.recreate_missing_stateful_set(cluster, logger)

        prepare.assert_called_once_with(cluster, cluster.parsed_spec, logger)
        adopt.assert_called_once_with(statefulset)
        create.assert_called_once_with(namespace="test-ns", body=statefulset)

    def test_tolerates_concurrent_creation(self):
        cluster = MagicMock(namespace="test-ns", name="test-cluster")
        logger = MagicMock()

        with patch.object(operator_cluster.cluster_objects,
                          "prepare_cluster_stateful_set",
                          return_value={}), \
             patch.object(operator_cluster.kopf, "adopt"), \
             patch.object(
                 operator_cluster.api_apps,
                 "create_namespaced_stateful_set",
                 side_effect=ApiException(status=409)):
            operator_cluster.recreate_missing_stateful_set(cluster, logger)

    def test_propagates_other_api_errors(self):
        cluster = MagicMock(namespace="test-ns", name="test-cluster")
        logger = MagicMock()

        with patch.object(operator_cluster.cluster_objects,
                          "prepare_cluster_stateful_set",
                          return_value={}), \
             patch.object(operator_cluster.kopf, "adopt"), \
             patch.object(
                 operator_cluster.api_apps,
                 "create_namespaced_stateful_set",
                 side_effect=ApiException(status=500)):
            with self.assertRaises(ApiException) as raised:
                operator_cluster.recreate_missing_stateful_set(cluster, logger)

        self.assertEqual(raised.exception.status, 500)


class DatadirVolumeClaimTemplateTest(unittest.TestCase):
    def setUp(self):
        self.cluster = MagicMock()
        self.cluster.ready = True
        self.cluster.name = "test-cluster"
        self.cluster.get_stateful_set.return_value.metadata.name = "test-cluster"
        self.cluster.get_stateful_set.return_value.metadata.namespace = "test-ns"
        self.logger = MagicMock()

    def test_storage_class_change_orphan_recreates_sts_without_expanding_pvcs(self):
        old = {
            "storageClassName": "standard",
            "resources": {"requests": {"storage": "10Gi"}},
        }
        new = {
            "storageClassName": "premium",
            "resources": {"requests": {"storage": "10Gi"}},
        }

        with patch.object(operator_cluster.cluster_objects,
                          "recreate_stateful_set") as recreate, \
             patch.object(operator_cluster.cluster_objects,
                          "expand_pvcs_and_recreate_sts") as expand:
            operator_cluster.on_innodbcluster_field_datadir_volume_claim_template(
                old, new, MagicMock(), self.cluster, MagicMock(), self.logger)

        recreate.assert_called_once_with(
            self.cluster, "test-cluster", "test-ns", self.logger,
            reason="storageClassName changed from 'standard' to 'premium'")
        expand.assert_not_called()

    def test_size_and_storage_class_change_expand_and_recreate_only_once(self):
        old = {
            "storageClassName": "standard",
            "resources": {"requests": {"storage": "10Gi"}},
        }
        new = {
            "storageClassName": "premium",
            "resources": {"requests": {"storage": "20Gi"}},
        }

        with patch.object(operator_cluster.cluster_objects,
                          "recreate_stateful_set") as recreate, \
             patch.object(operator_cluster.cluster_objects,
                          "expand_pvcs_and_recreate_sts") as expand:
            operator_cluster.on_innodbcluster_field_datadir_volume_claim_template(
                old, new, MagicMock(), self.cluster, MagicMock(), self.logger)

        expand.assert_called_once_with(self.cluster, "20Gi", self.logger)
        recreate.assert_not_called()


class RecreateStatefulSetTest(unittest.TestCase):
    def test_delete_uses_orphan_propagation_and_recreates_current_template(self):
        cluster = MagicMock()
        cluster.parsed_spec = MagicMock()
        logger = MagicMock()
        statefulset = {"metadata": {"name": "test-cluster"}}

        with patch.object(operator_cluster.cluster_objects.api_apps,
                          "delete_namespaced_stateful_set") as delete, \
             patch.object(
                 operator_cluster.cluster_objects.api_apps,
                 "read_namespaced_stateful_set",
                 side_effect=ApiException(status=404)), \
             patch.object(
                 operator_cluster.cluster_objects,
                 "prepare_cluster_stateful_set",
                 return_value=statefulset) as prepare, \
             patch.object(operator_cluster.cluster_objects.kopf,
                          "adopt") as adopt, \
             patch.object(operator_cluster.cluster_objects.api_apps,
                          "create_namespaced_stateful_set") as create:
            operator_cluster.cluster_objects.recreate_stateful_set(
                cluster, "test-cluster", "test-ns", logger,
                reason="storageClassName changed")

        delete.assert_called_once()
        delete_options = delete.call_args.kwargs["body"]
        self.assertEqual(delete_options.propagation_policy, "Orphan")
        prepare.assert_called_once_with(cluster, cluster.parsed_spec, logger)
        adopt.assert_called_once_with(statefulset)
        create.assert_called_once_with(namespace="test-ns", body=statefulset)
