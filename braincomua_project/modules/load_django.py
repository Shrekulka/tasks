# braincomua_project/modules/load_django.py

"""
Bridge file: initializes Django ORM so standalone scripts in the modules/
folder can import and use models from parser_app, exactly like inside
the Django project itself.
"""
import os
import sys

import django

# Path to the modules directory (where this file is located)
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

# Path to the project root (one level above modules/)
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, '..'))

# Add the project root to the Python path for module lookup
sys.path.append(PROJECT_ROOT)

# Tell Django where to find settings.py
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Run Django initialization (registers applications, models, etc.)
django.setup()
