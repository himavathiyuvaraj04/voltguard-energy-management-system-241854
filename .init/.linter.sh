#!/bin/bash
cd /home/kavia/workspace/code-generation/voltguard-energy-management-system-241854/flask_backend
source venv/bin/activate
flake8 .
LINT_EXIT_CODE=$?
if [ $LINT_EXIT_CODE -ne 0 ]; then
  exit 1
fi

