import ast
import unittest
from pathlib import Path


class KopfDecoratorCompatibilityTest(unittest.TestCase):
    def test_delete_decorators_do_not_use_unsupported_eager_argument(self):
        operator_cluster = (
            Path(__file__).parents[2]
            / "mysqloperator/controller/innodbcluster/operator_cluster.py"
        )
        tree = ast.parse(operator_cluster.read_text(encoding="utf-8"))

        eager_delete_decorators = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "delete"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "on"
            ):
                continue
            if any(keyword.arg == "eager" for keyword in node.keywords):
                eager_delete_decorators.append(node.lineno)

        self.assertEqual(eager_delete_decorators, [])
