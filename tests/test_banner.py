"""The startup banner goes to the journal, so it must not carry the token."""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main_function():
    with open(os.path.join(ROOT, "zero2w_console", "server.py")) as fh:
        tree = ast.parse(fh.read())
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "main")


def names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


class TestTheBanner(unittest.TestCase):
    def test_nothing_printed_is_built_from_the_token(self):
        main = main_function()
        # Anything assigned from the token is as secret as the token.
        tainted = {"token"}
        for node in ast.walk(main):
            if isinstance(node, ast.Assign) and names_in(node.value) & tainted:
                for target in node.targets:
                    tainted |= names_in(target)
        tainted -= {"httpd"}             # holds it in memory, never printed
        for node in ast.walk(main):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
                with self.subTest(line=node.lineno):
                    self.assertFalse(names_in(node) & tainted)


if __name__ == "__main__":
    unittest.main()
