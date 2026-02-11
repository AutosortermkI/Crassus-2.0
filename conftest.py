"""
Root conftest -- adds function_app/ to sys.path so tests can import modules.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "function_app"))
