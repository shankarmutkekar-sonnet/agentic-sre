#!/bin/bash
set -e

echo "Installing dependencies..."
sudo yum install -y python3-pip
pip3 install flask boto3 gunicorn --user
echo "Dependencies installed successfully"
