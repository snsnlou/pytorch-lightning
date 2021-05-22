"""
Add "reveal_locals()" to each function def
keeping the type comments
"""
from typed_ast import ast3
import sys
import typed_astunparse
from typing import List, Tuple
import csv
import os

def add_reveal_locals(contents: str) -> str:
    tree = None
    try:
        tree = ast3.parse(contents)
    except:
        return contents
    for node in ast3.walk(tree):
        if isinstance(node, ast3.FunctionDef) or isinstance(node, ast3.AsyncFunctionDef):
            reveal_locals_call = ast3.Expr(value=ast3.Call(func=ast3.Name(id='reveal_locals', ctx=ast3.Load()), args=[], keywords=[]))
            if isinstance(node.body[-1], ast3.Return):
                node.body.insert(-1, reveal_locals_call)
            else:
                node.body.append(reveal_locals_call)
    
    return typed_astunparse.unparse(tree)


def get_reveal_locals_location(contents: str) -> List[Tuple[str, int]]:
    tree = None
    res = []
    try:
        tree = ast3.parse(contents)
    except:
        return res
    
    stack = [('', child) for child in tree.body[::-1]]
    while stack:
        parent_name, node = stack.pop()
        if isinstance(node, ast3.FunctionDef) or isinstance(node, ast3.AsyncFunctionDef):
            full_name = '{}.{}'.format(parent_name, node.name) if parent_name else node.name
            for child in node.body:
                if isinstance(child, ast3.Expr) and isinstance(child.value, ast3.Call) and isinstance(child.value.func, ast3.Name) and child.value.func.id == 'reveal_locals':
                    res.append((full_name, child.lineno))
            for child in node.body[::-1]:
                stack.append((full_name, child))
        elif isinstance(node, ast3.ClassDef):
            full_name = '{}.{}'.format(parent_name, node.name) if parent_name else node.name
            for child in node.body[::-1]:
                stack.append((full_name, child))
    
    return res

def process_reveal_locals() -> None:
    csv_file = open('reveal_locals_location.csv', 'w')
    csv_writer = csv.writer(csv_file)
    excepted_files = ['typecheck.py', 'add_reveal_locals_typed.py']
    for root, dirs, files in os.walk('.'):
        dirs.sort()
        for file_name in sorted(files):
            if not file_name.endswith('.py'):
                continue
            excepted = False
            full_path = os.path.join(root, file_name)
            for excepted_file in excepted_files:
                if os.path.samefile(excepted_file, full_path):
                    excepted = True
                    break
            if excepted:
                continue
            file = open(full_path)
            contents = ''.join(file.readlines())
            file.close()
            new_contents = add_reveal_locals(contents)
            reveal_locals_location = get_reveal_locals_location(new_contents)
            for location in reveal_locals_location:
                csv_writer.writerow([full_path, location[0], location[1]])
            file = open(full_path, 'w')
            file.write(new_contents)
            file.close()
            
    csv_file.close()
