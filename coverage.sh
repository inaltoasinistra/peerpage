#!/bin/bash
source venv/bin/activate
coverage run -m unittest discover -v
coverage report
coverage xml
