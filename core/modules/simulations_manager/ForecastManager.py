# -*- coding: utf-8 -*-
import os
import re
import shutil
import threading
import logging
import copy
from datetime import datetime, timedelta

from core.lib.io.file import create_folder_with_permissions, listdir_fullpath
from core.lib.utils.log import log_format_exception
from core.modules.simulations_manager.CampaignWriter import CampaignWriter
from core.modules.simulations_manager.weather.CombinedSeriesMaker import CombinedSeriesMaker
# from core.modules.simulations_manager.weather.csv.CombinedSeriesMaker import CSVCombinedSeriesMaker
from core.modules.simulations_manager.weather.HistoricalSeriesMaker import HistoricalSeriesMaker
from core.modules.simulations_manager.RunpSIMS import RunpSIMS
from core.lib.jobs.monitor import NullMonitor, JOB_STATUS_WAITING, JOB_STATUS_RUNNING, ProgressMonitor
from core.modules.config.priority import RUN_FORECAST, RUN_REFERENCE_FORECAST

__author__ = 'Federico Schmidt'


class ForecastManager:

    def __init__(self, scheduler, system_config, weather_updater):
        self.system_config = system_config
        self.psims_runner = RunpSIMS()
        self.scheduler = scheduler
        self.weather_updater = weather_updater
        self.scheduled_reference_simulations_ids = set()

    def start(self):
        for file_name, forecast_list in self.system_config.forecasts.iteritems():
            for forecast in forecast_list:
                self.schedule_forecast(forecast)

    def __run__(self):
        # Create scheduler, etc.
        for forecast in self.system_config.forecasts:
            self.run_forecast(forecast)

    def schedule_forecast(self, forecast, priority=RUN_FORECAST):
        job_name = "%s (%s)" % (forecast.name, forecast.forecast_date)
        # Get MongoDB connection.
        db = self.system_config.database['yield_db']

        existing = db.forecasts.find_one({'_id': forecast.id})
        if existing:
            logging.debug('Skipping forecast "%s" (id: %s) because it was already executed.' % (
                job_name, forecast.id
            ))
            return

        run_date = datetime.strptime(forecast.forecast_date, '%Y-%m-%d')

        if run_date < datetime.now():
            # Run immediately.
            job_handle = self.scheduler.add_job(self.run_forecast, name=job_name, args=[forecast],
                                                kwargs={'priority': priority})
        else:
            # Schedule for running at the specified date at 19:00.
            run_date = run_date.replace(hour=19)

            # scheduler.add_job(Worker.create_and_run, 'interval', seconds=5, args=[job_0, scheduler])
            job_handle = self.scheduler.add_job(self.run_forecast, trigger='date', name=job_name, args=[forecast],
                                                kwargs={'priority': priority}, run_date=run_date)
        forecast['job_id'] = job_handle.id

    def reschedule_forecast(self, forecast, now=False):
        job_name = "Rescheduled: %s (%s)" % (forecast.name, forecast.forecast_date)
        if now:
            new_run_date = datetime.now()
        else:
            new_run_date = datetime.now() + timedelta(days=1)
        job_handle = self.scheduler.add_job(self.run_forecast, trigger='date', name=job_name, args=[forecast],
                                            run_date=new_run_date)
        forecast['job_id'] = job_handle.id

    def run_forecast(self, yield_forecast, priority=RUN_FORECAST, progress_monitor=None):
        forecast_full_name = '%s (%s)' % (yield_forecast.name, yield_forecast.forecast_date)
        logging.getLogger().info('Running forecast "%s".' % forecast_full_name)

        psims_exit_code = None
        db = None
        forecast_id = None
        simulations_ids = None
        exception_raised = False

        if not progress_monitor:
            progress_monitor = NullMonitor

        progress_monitor.end_value = 5
        progress_monitor.job_started()
        progress_monitor.update_progress(job_status=JOB_STATUS_WAITING)

        with self.system_config.jobs_lock.blocking_job(priority=priority):
            # Lock acquired.
            progress_monitor.update_progress(job_status=JOB_STATUS_RUNNING)

            forecast = copy.deepcopy(yield_forecast)
            try:
                run_start_time = datetime.now()

                # Get MongoDB connection.
                db = self.system_config.database['yield_db']

                wth_series_maker = None
                forecast.configuration['simulation_collection'] = 'simulations'

                if forecast.configuration.weather_series == 'combined':
                    wth_series_maker = CombinedSeriesMaker(self.system_config, forecast.configuration.max_parallelism)
                elif forecast.configuration.weather_series == 'historic':
                    wth_series_maker = HistoricalSeriesMaker(self.system_config, forecast.configuration.max_parallelism)
                    forecast.configuration['simulation_collection'] = 'reference_simulations'
                else:
                    raise RuntimeError('Weather series type "%s" unsupported.' % forecast.configuration.weather_series)

                folder_name = "%s" % (datetime.now().isoformat())
                folder_name = folder_name.replace('"', '').replace('\'', '').replace(' ', '_')
                forecast.folder_name = folder_name.encode('unicode-escape')

                # Add folder name to rundir and create it.
                forecast.paths.rundir = os.path.abspath(os.path.join(forecast.paths.rundir,
                                                                     folder_name)).encode('unicode-escape')
                create_folder_with_permissions(forecast.paths.rundir)

                # Create a folder for the weather grid inside that rundir.
                forecast.paths.weather_grid_path = os.path.join(forecast.paths.rundir, 'wth')
                create_folder_with_permissions(forecast.paths.weather_grid_path)

                # Create a folder for the soil grid inside that rundir.
                forecast.paths.soil_grid_path = os.path.join(forecast.paths.rundir, 'soils')
                create_folder_with_permissions(forecast.paths.soil_grid_path)

                # Create the folder where we'll read the CSV files created by the database.
                forecast.paths.wth_csv_read = os.path.join(forecast.paths.wth_csv_read, folder_name).encode('unicode-escape')
                forecast.paths.wth_csv_export = os.path.join(forecast.paths.wth_csv_export, folder_name).encode('unicode-escape')
                create_folder_with_permissions(forecast.paths.wth_csv_read)

                active_threads = dict()

                forecast.weather_stations = {}
                forecast.rainfall = {}

                stations_not_updated = set()
                run_date = datetime.strptime(forecast.forecast_date, '%Y-%m-%d').date()

                for loc_key, location in forecast['locations'].iteritems():
                    omm_id = location['weather_station']

                    # Upsert location.
                    db.locations.update_one({'_id': location.id}, {
                        '$set': {
                            "name": location.name,
                            "coord_x": location.coord_x,
                            "coord_y": location.coord_y,
                            "weather_station": location.weather_station
                        }
                    }, upsert=True)

                    if omm_id not in self.weather_updater.wth_max_date:
                        self.weather_updater.add_weather_station_id(omm_id)
                        stations_not_updated.add(omm_id)
                        continue
                    elif self.weather_updater.wth_max_date[omm_id] < run_date:
                        stations_not_updated.add(omm_id)
                        continue
                    elif omm_id not in active_threads:
                        # Weather station data updated, forecast can be ran.
                        active_threads[omm_id] = threading.Thread(target=wth_series_maker.create_series,
                                                                  name='create_series for omm_id = %s' % omm_id,
                                                                  args=(location, forecast))
                    else:
                        # Weather station already has an associated thread that will create the weather series.
                        continue

                if len(stations_not_updated) > 0:
                    # Forecast can't continue, must be rescheduled.
                    logging.warning("Couldn't run forecast \"%s\" because the following weather stations don't have "
                                    "updated data: %s." % (forecast_full_name, list(stations_not_updated)))
                    self.reschedule_forecast(forecast)
                    return 0

                progress_monitor.update_progress(new_value=1)

                weather_series_monitor = ProgressMonitor(end_value=len(active_threads))
                progress_monitor.add_subjob(weather_series_monitor, job_name='Create weather series')
                joined_threads_count = 0

                # Start all weather maker threads.
                for t in active_threads.values():
                    t.start()

                # Wait for the weather grid to be populated.
                for t in active_threads.values():
                    t.join()
                    joined_threads_count += 1
                    weather_series_monitor.update_progress(joined_threads_count)

                weather_series_monitor.job_ended()
                progress_monitor.update_progress(new_value=2)

                # If the folder is empty, delete it.
                if len(os.listdir(forecast.paths.wth_csv_read)) == 0:
                    shutil.rmtree(forecast.paths.wth_csv_read)

                forecast_persistent_view = forecast.persistent_view()
                is_reference_forecast = True
                if forecast_persistent_view:
                    is_reference_forecast = False
                    forecast_id = db.forecasts.insert_one(forecast_persistent_view).inserted_id

                    if not forecast_id:
                        raise RuntimeError('Failed to insert forecast with id: %s' % forecast_persistent_view['_id'])

                simulations_ids = []
                reference_ids = []

                # Flatten simulations and update location info (with id's and computed weather stations).
                for loc_key, loc_simulations in forecast.simulations.iteritems():
                    for sim in loc_simulations:
                        sim.location = forecast.locations[loc_key]
                        sim.weather_station = forecast.weather_stations[sim.location.weather_station]

                        # If a simulation has an associated forecast, it should go inside the 'simulations' collection.
                        if forecast_id:
                            sim.forecast_id = forecast_id
                            sim.forecast_date = forecast.forecast_date
                            sim_id = db.simulations.insert_one(sim.persistent_view()).inserted_id
                            reference_ids.append(sim.reference_id)
                        else:
                            # Otherwise, it means it's a reference (historic) simulation.
                            sim_id = db.reference_simulations.insert_one(sim.persistent_view()).inserted_id
                        sim['_id'] = sim_id
                        simulations_ids.append(sim_id)

                if not is_reference_forecast:
                    # Find which simulations have a reference simulation associated.
                    found_reference_simulations = db.reference_simulations.find({
                        '_id': {
                            '$in': reference_ids
                        }
                    }, projection=['_id'])

                    found_reference_simulations = set([s['_id'] for s in found_reference_simulations])

                    diff = set(reference_ids) - found_reference_simulations - self.scheduled_reference_simulations_ids
                    if len(diff) > 0:
                        # There are simulations that don't have a reference simulation calculated.
                        ref_forecast = copy.deepcopy(yield_forecast)
                        ref_forecast.name = 'Reference simulations for forecast %s' % forecast.name
                        ref_forecast.configuration.weather_series = 'historic'

                        rm_locs = []

                        for loc_key, loc_simulations in ref_forecast.simulations.iteritems():
                            # Filter reference simulations.
                            loc_simulations[:] = [x for x in loc_simulations if x.reference_id in diff]

                            if len(loc_simulations) == 0:
                                rm_locs.append(loc_key)

                        for loc_key in rm_locs:
                            del ref_forecast.locations[loc_key]
                            del ref_forecast.simulations[loc_key]

                        self.schedule_forecast(ref_forecast, priority=RUN_REFERENCE_FORECAST)
                        self.scheduled_reference_simulations_ids |= diff
                        logging.info('Scheduled reference simulations for forecast: %s' % forecast.name)
                else:
                    # Remove this reference forecasts id's.
                    self.scheduled_reference_simulations_ids -= set(reference_ids)

                progress_monitor.update_progress(new_value=3)

                forecast.paths.run_script_path = CampaignWriter.write_campaign(forecast, output_dir=forecast.paths.rundir)
                forecast.simulation_count = len(simulations_ids)

                progress_monitor.update_progress(new_value=4)

                # Insertar ID's de simulaciones en el pronóstico.
                if forecast_id:
                    db.forecasts.update_one(
                        {"_id": forecast_id},
                        {"$pushAll": {
                            "simulations": simulations_ids
                        }}
                    )

                # Ejecutar simulaciones.
                weather_series_monitor = ProgressMonitor()
                progress_monitor.add_subjob(weather_series_monitor, job_name='Run pSIMS')
                psims_exit_code = self.psims_runner.run(forecast, progress_monitor=weather_series_monitor, verbose=True)

                # Check results
                if psims_exit_code == 0:
                    inserted_count = db[forecast.configuration['simulation_collection']].count({
                        '_id': {'$in': simulations_ids}
                    })

                    if len(simulations_ids) != inserted_count:
                        raise RuntimeError('Mismatch between simulations id\'s length and inserted id\'s '
                                           'length (%s != %s)' % (len(simulations_ids), inserted_count))

                logging.getLogger().info('Finished running forecast "%s" (time=%s).\n' %
                                         (forecast.name, datetime.now() - run_start_time))
            except:
                logging.getLogger().error("Failed to run forecast '%s'. Reason: %s" %
                                          (forecast.name, log_format_exception()))

                exception_raised = True
            finally:
                if exception_raised or psims_exit_code != 0:
                    logging.info('Rolling back DB data for forecast "%s".' % forecast_full_name)
                    if db:
                        if simulations_ids and len(simulations_ids) > 0:
                            db[forecast.configuration['simulation_collection']].delete_many(
                                {"_id": {"$in": simulations_ids}}
                            )
                        if forecast_id:
                            db.forecasts.delete_one({"_id": forecast_id})
                    return -1

                if not psims_exit_code or psims_exit_code == 0:
                    # Clean the rundir.
                    if os.path.exists(forecast.paths.rundir):
                        shutil.rmtree(forecast.paths.rundir)

                if psims_exit_code == 0:
                    # Clean pSIMS run folder.
                    rundir_regex = re.compile('.+/run(\d){3}$')
                    files_filter = lambda file_name: rundir_regex.match(file_name) is not None

                    psims_run_dirs = sorted(listdir_fullpath(forecast.paths.psims, filter=files_filter),
                                            reverse=True)

                    if len(psims_run_dirs) > 0:
                        # Remove the last runNNN directory (the one this execution created).
                        shutil.rmtree(psims_run_dirs[0])

                return psims_exit_code