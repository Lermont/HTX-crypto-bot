import ast

with open('htxbot/monitoring.py', 'r') as f:
    tree = ast.parse(f.read())

for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_record_signal_analytics':
        for arg in node.args.args:
            print(arg.arg)
