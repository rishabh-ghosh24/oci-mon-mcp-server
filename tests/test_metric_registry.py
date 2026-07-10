import unittest
from oci_mon_mcp.metric_registry import MetricRegistry

class MetricRegistryTests(unittest.TestCase):
    def test_load_registry_and_resolve_cpu(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve("cpu")
        self.assertEqual(entry.namespace, "oci_computeagent")
        self.assertEqual(entry.metric_names, ("CpuUtilization",))
        self.assertEqual(entry.label, "CPU utilization")
        self.assertEqual(entry.y_axis, "cpu_utilization_percent")

    def test_resolve_unknown_key_returns_none(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve("nonexistent_metric")
        self.assertIsNone(entry)

    def test_resolve_by_alias_finds_memory(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve_by_alias("show me the ram usage")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metric_key, "memory")
        self.assertEqual(entry.namespace, "oci_computeagent")

    def test_resolve_by_alias_finds_network(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve_by_alias("show network ingress bytes")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metric_key, "vcn_bytes_in")
        self.assertEqual(entry.namespace, "oci_vcn")

    def test_list_namespaces_returns_all(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        namespaces = registry.list_namespaces()
        self.assertIn("oci_computeagent", namespaces)
        self.assertIn("oci_vcn", namespaces)
        self.assertIn("oci_database", namespaces)
        self.assertGreaterEqual(len(namespaces), 8)

    def test_namespace_info_includes_resource_type(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        info = registry.get_namespace_info("oci_database")
        self.assertIsNotNone(info)
        self.assertEqual(info.resource_type, "DbSystem")
        self.assertEqual(info.sdk_client, "DatabaseClient")

    def test_stack_monitoring_metric_includes_resource_group(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve("ebs_active_users")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.namespace, "oracle_appmgmt")
        self.assertEqual(entry.resource_group, "ebs_instance")
        self.assertEqual(entry.metric_names, ("ActiveUserSessions",))

        responsibility_entry = registry.resolve("ebs_active_users_by_responsibility")
        self.assertIsNotNone(responsibility_entry)
        self.assertEqual(
            responsibility_entry.metric_names,
            ("ActiveUserSessionsByResponsibility",),
        )
        self.assertEqual(responsibility_entry.resource_group, "ebs_instance")

        info = registry.get_namespace_info("oracle_appmgmt")
        self.assertIsNotNone(info)
        self.assertEqual(info.resource_name_dimension, "resourceName")
        self.assertEqual(info.group_by_dimensions, ("resourceId", "resourceName"))

    def test_managed_database_namespace_uses_resource_name_dimension(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve("managed_db_time")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.namespace, "oracle_oci_database")
        self.assertIsNone(entry.resource_group)

        info = registry.get_namespace_info("oracle_oci_database")
        self.assertIsNotNone(info)
        self.assertEqual(info.resource_name_dimension, "resourceName")

    def test_database_cluster_metric_includes_component_resource_group(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve("db_listener_refused_connections")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.namespace, "oracle_oci_database_cluster")
        self.assertEqual(entry.resource_group, "oracle_lsnr")
        self.assertEqual(entry.metric_names, ("RefusedConnections",))

    def test_alias_disambiguation_oke_cpu_vs_cpu(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve_by_alias("show oke cpu utilization")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metric_key, "oke_node_cpu")
        self.assertEqual(entry.namespace, "oci_oke")

    def test_alias_disambiguation_db_cpu_vs_cpu(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve_by_alias("database cpu utilization")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metric_key, "db_cpu")
        self.assertEqual(entry.namespace, "oci_database")

    def test_runtime_fallback_discovers_unknown_namespace(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        discovered = registry.register_discovered_namespace(
            namespace="oci_custom_namespace",
            display_name="Custom Service",
            metrics=[
                {"metric_name": "CustomMetric1", "unit": "count"},
                {"metric_name": "CustomMetric2", "unit": "percent"},
            ],
        )
        self.assertIsNotNone(discovered)
        self.assertEqual(discovered.namespace, "oci_custom_namespace")
        entry = registry.resolve("oci_custom_namespace__CustomMetric1")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.namespace, "oci_custom_namespace")
        self.assertEqual(entry.metric_names, ("CustomMetric1",))
        self.assertIsNone(entry.resource_group)

    def test_all_entries_have_required_fields(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        for key in registry.all_metric_keys:
            entry = registry.resolve(key)
            self.assertIsNotNone(entry, f"Missing entry for key: {key}")
            self.assertTrue(entry.label, f"Empty label for key: {key}")
            self.assertTrue(entry.namespace, f"Empty namespace for key: {key}")
            self.assertTrue(entry.metric_names, f"Empty metric_names for key: {key}")
            self.assertTrue(entry.y_axis, f"Empty y_axis for key: {key}")
