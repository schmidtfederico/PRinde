from core.lib.io.file import listdir_fullpath
from core.lib.utils.DotDict import DotDict
from core.lib.utils.log import log_format_exception
from core.model.ForecastBuilder import ForecastBuilder

__author__ = 'Federico Schmidt'

import yaml
import os.path
import psycopg2
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import threading
import logging
import logging.config


class Configuration(FileSystemEventHandler):

    def __init__(self, root_path):
        self.root_path = root_path
        self.config_path = os.path.join(root_path, 'config')

        # Create system configuration path relative to root.
        self.system_config_path = os.path.join(self.config_path, 'system.yaml')
        self.alias_keys_path = os.path.join(self.config_path, 'alias.json')
        self.databases_config_path = os.path.join(self.config_path, 'database.yaml')
        self.forecasts_path = os.path.join(self.config_path, 'forecasts')

        if not os.path.isfile(self.system_config_path) or not os.path.exists(self.system_config_path):
            raise RuntimeError('System configuration file not found.')

        if not os.path.isfile(self.databases_config_path) or not os.path.exists(self.databases_config_path):
            raise RuntimeError('Database configuration file not found ("%s").' % self.databases_config_path)

        if not os.path.isfile(self.alias_keys_path):
            logging.getLogger('main').warning("Alias keys file not found, forecasts keys must match pSIMS keys.")
            self.alias_keys_path = None

        # Find and validate JSON schema for Simulation objects.
        self.simulation_schema_path = os.path.join(self.config_path, 'schema', 'simulation.json')
        if not os.path.isfile(self.simulation_schema_path) or not os.path.exists(self.simulation_schema_path):
            raise RuntimeError('Simulation schema JSON file not found ("%s").' % self.simulation_schema_path)

        self.log_format = '%(asctime)s - %(module)s:%(lineno)s - %(levelname)s - %(message)s'

        self.logger = logging.getLogger('main')
        self.logger.setLevel(logging.DEBUG)

        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(logging.Formatter(self.log_format))
        self.logger.addHandler(ch)

        fh = logging.FileHandler('run.log', mode='w')
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(self.log_format))
        self.logger.addHandler(fh)

        self.watch_thread = None
        self.observer = None

    def load(self):
        # Load system configuration from the YAML file and update this object's dictionary to add the keys found
        # in the config file, allowing us to access them using the dot notation (eg. "config.temp_folder" instead of
        # "config.get('temp_folder')", though this is also supported.
        system_config = DotDict(yaml.safe_load(open(self.system_config_path)))

        if 'max_paralellism' not in system_config:
            system_config.max_paralellism = 4
        else:
            if (not isinstance(system_config.max_paralellism, int)) or (system_config.max_paralellism < 1):
                raise RuntimeError('Invalid max_paralellism value (%s).' % system_config.max_paralellism)

        self.__dict__.update(system_config)

        # Load databases configurations and open connections.
        db_config = yaml.safe_load(open(self.databases_config_path))

        self.__dict__['database'] = dict()

        for db_conn, properties in db_config.items():
            if 'db_name' not in properties:
                raise RuntimeError('Missing database name for database connection "%s".' %
                                   db_conn)
            if 'user' not in properties:
                raise RuntimeError('Missing user name for database connection "%s".' %
                                   db_conn)

            if 'port' not in properties:
                properties['port'] = 5432
            if 'host' not in properties:
                properties['host'] = 'localhost'

            if 'password' not in properties:
                pwd_file_path = os.path.join(self.config_path, 'pwd', properties['user']+'.pwd')
                if not os.path.isfile(pwd_file_path):
                    raise RuntimeError('Missing password or password file for database connection "%s".'
                                       % db_conn)

                properties['password'] = open(pwd_file_path, mode='r').read()

            try:
                conn = psycopg2.connect(host=properties['host'],
                                        user=properties['user'],
                                        database=properties['db_name'],
                                        port=properties['port'],
                                        password=properties['password'])

                self.__dict__['database'][db_conn] = conn
            except Exception as e:
                raise RuntimeError('Failed to create database connection "%s". Reason: "%s".' %
                                   (db_conn, e.message))

        # Load forecasts.
        self.__dict__['forecasts'] = []

        alias_dict = None
        if self.alias_keys_path:
            alias_dict = yaml.load(open(self.alias_keys_path, 'r'))

        for file_name in listdir_fullpath(self.forecasts_path,
                                          onlyFiles=True,
                                          recursive=True,
                                          filter=(lambda x: x.endswith('yaml'))):
            forecast = DotDict(yaml.safe_load(open(file_name)))
            forecast['file_name'] = file_name

            try:
                builder = ForecastBuilder(forecast, self.simulation_schema_path)
                builder.replace_alias(alias_dict)
                builder.inherit_config(system_config)
                forecasts = builder.build()

                self.__dict__['forecasts'].append(forecasts)
            except Exception:
                logging.getLogger('main').error("Skipping forecast file '%s'. Reason: %s." %
                                                (file_name, log_format_exception()))

        # If the watch thread isn't already loaded, create and start it.
        if not self.watch_thread:
            # Create a Thread with "watch" function and start it.
            self.watch_thread = threading.Thread(target=self.watch)
            # self.watch_thread.start()

    def get(self, key, default=None):
        """
        Provides a Python's dictionary alike way of accessing configuration properties, with default values in case
        of missingness.
        :param key: The key you're looking for in the configuration.
        :param default: The value that should be returned if the key is not found.
        :return: The value associated with the key provided, or the default value if not found.
        """
        return self.__dict__.get(key, default)

    def watch(self):
        """
        This function is a new thread, instantiates the watchdog.Observer thread and registers this object (self)
        as the Event Handler (see watchdog's documentation).
        :return: None
        """
        # We'll watch the system's config parent path.
        watch_path = os.path.dirname(self.config_path)

        self.observer = Observer()
        self.observer.schedule(self, watch_path, recursive=True)
        self.observer.start()
        self.observer.join()

    def on_modified(self, event):
        """
        Overrides the FileSystemEventHandler.on_modified function, will be triggered every time a file or directory
        changes inside the configuration folder.
        :param event: The object representing the event, see Watchdog's documentation.
        :return: None
        """
        if not event.is_directory and \
                event.src_path.startswith(self.config_path) and \
                event.src_path.endswith('yaml'):
            # Reload config.
            self.load()
        pass