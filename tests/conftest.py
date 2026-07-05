"""
Shared fixtures. Every fixture here exists to keep tests from ever touching
real app data (~/lumina/memory/*, ~/.config/lumina/*) — each test gets its
own throwaway directory.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
