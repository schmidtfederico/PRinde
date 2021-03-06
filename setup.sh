#!/bin/sh

MONGO_NOT_FOUND=$(service mongod status 2>&1 | grep unrecognized)

# If not mongo...
if [ ! -z "$MONGO_NOT_FOUND" ]; then
    echo "Mongo not installed..."
    sudo apt-key adv --keyserver hkp://keyserver.ubuntu.com:80 --recv 7F0CEB10
    echo 'deb http://downloads-distro.mongodb.org/repo/ubuntu-upstart dist 10gen' | sudo tee /etc/apt/sources.list.d/mongodb.list
    sudo apt-get update
    sudo apt-get install -y mongodb-org
    sudo service mongod start
fi

# Install Postgres
# sudo apt-get install -y postgresql-9.3 postgresql-contrib-9.3

# Setup
sudo apt-get install -y build-essential python-dev python-pip
sudo apt-get install -y python-psycopg2

sudo pip install watchdog
sudo pip install requests
sudo pip install functools32
sudo pip install jsonschema
sudo pip install pymongo
sudo pip install apscheduler
sudo pip install Flask
sudo pip install Flask-SocketIO==0.6.0
