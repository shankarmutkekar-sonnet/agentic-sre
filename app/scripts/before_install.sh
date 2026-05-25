#!/bin/bash
set -e

# Install Python and pip if not present
yum install -y python3 python3-pip 2>/dev/null || apt-get install -y python3 python3-pip

# Install app dependencies
pip3 install -r /opt/flask-app/requirements.txt
