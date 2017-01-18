# -*- coding: utf-8 -*-
from __future__ import absolute_import
import datetime
from time import sleep
from celery.utils.log import get_task_logger
from celery.exceptions import SoftTimeLimitExceeded
from django.db.models import Sum, Count
from django.utils.module_loading import import_by_path
from dbaas.celery import app
from account.models import Team
from logical.models import Database
from physical.models import Plan, DatabaseInfra, Instance
from util import email_notifications, get_worker_name, full_stack
from util.decorators import only_one
from util.providers import make_infra, clone_infra, destroy_infra, \
    get_database_upgrade_setting
from simple_audit.models import AuditRequest
from system.models import Configuration
from .models import TaskHistory

LOG = get_task_logger(__name__)


def get_history_for_task_id(task_id):
    try:
        return TaskHistory.objects.get(task_id=task_id)
    except Exception:
        LOG.error("could not find history for task id %s" % task_id)
        return None


def rollback_database(dest_database):
    dest_database.is_in_quarantine = True
    dest_database.save()
    dest_database.delete()


@app.task(bind=True)
def create_database(
    self, name, plan, environment, team, project, description,
    subscribe_to_email_events=True, task_history=None, user=None,
    is_protected=False
):
    AuditRequest.new_request("create_database", user, "localhost")
    try:

        worker_name = get_worker_name()
        task_history = TaskHistory.register(
            request=self.request, task_history=task_history, user=user,
            worker_name=worker_name
        )

        LOG.info(
            "id: %s | task: %s | kwargs: %s | args: %s" % (
                self.request.id, self.request.task, self.request.kwargs,
                str(self.request.args)
            )
        )

        task_history.update_details(persist=True, details="Loading Process...")

        result = make_infra(
            plan=plan, environment=environment, name=name, team=team,
            project=project, description=description,
            subscribe_to_email_events=subscribe_to_email_events,
            task=task_history, is_protected=is_protected
        )

        if result['created'] is False:
            if 'exceptions' in result:
                error = "\n".join(
                    ": ".join(err) for err in result['exceptions']['error_codes']
                )
                traceback = "\nException Traceback\n".join(
                    result['exceptions']['traceback']
                )
                error = "{}\n{}\n{}".format(error, traceback, error)
            else:
                error = "There is not any infra-structure to allocate this database."

            task_history.update_status_for(
                TaskHistory.STATUS_ERROR, details=error
            )
            return

        task_history.update_dbid(db=result['database'])
        task_history.update_status_for(
            TaskHistory.STATUS_SUCCESS, details='Database created successfully'
        )

        return

    except Exception as e:
        traceback = full_stack()
        LOG.error("Ops... something went wrong: %s" % e)
        LOG.error(traceback)

        if 'result' in locals() and result['created']:
            destroy_infra(
                databaseinfra=result['databaseinfra'], task=task_history)

        task_history.update_status_for(
            TaskHistory.STATUS_ERROR, details=traceback)
        return

    finally:
        AuditRequest.cleanup_request()


@app.task(bind=True)
def destroy_database(self, database, task_history=None, user=None):
    # register History
    AuditRequest.new_request("destroy_database", user, "localhost")
    try:
        worker_name = get_worker_name()
        task_history = TaskHistory.register(request=self.request, task_history=task_history,
                                            user=user, worker_name=worker_name)

        LOG.info("id: %s | task: %s | kwargs: %s | args: %s" % (
            self.request.id, self.request.task, self.request.kwargs, str(self.request.args)))

        task_history.update_details(persist=True, details="Loading Process...")

        databaseinfra = database.databaseinfra

        destroy_infra(databaseinfra=databaseinfra, task=task_history)

        task_history.update_status_for(
            TaskHistory.STATUS_SUCCESS, details='Database destroyed successfully')
        return
    finally:
        AuditRequest.cleanup_request()


@app.task(bind=True)
def clone_database(self, origin_database, clone_name, plan, environment, task_history=None, user=None):
    AuditRequest.new_request("clone_database", user, "localhost")
    try:
        worker_name = get_worker_name()
        LOG.info("id: %s | task: %s | kwargs: %s | args: %s" % (
            self.request.id, self.request.task, self.request.kwargs, str(self.request.args)))

        task_history = TaskHistory.register(request=self.request, task_history=task_history,
                                            user=user, worker_name=worker_name)

        LOG.info("origin_database: %s" % origin_database)

        task_history.update_details(persist=True, details="Loading Process...")
        result = clone_infra(
            plan=plan, environment=environment, name=clone_name,
            team=origin_database.team, project=origin_database.project,
            description=origin_database.description, task=task_history,
            clone=origin_database,
            subscribe_to_email_events=origin_database.subscribe_to_email_events
        )

        if result['created'] is False:
            if 'exceptions' in result:
                error = "\n\n".join(
                    ": ".join(err) for err in result['exceptions']['error_codes']
                )
                traceback = "\n\nException Traceback\n".join(
                    result['exceptions']['traceback'])
                error = "{}\n{}".format(error, traceback)
            else:
                error = "There is not any infra-structure to allocate this database."

            task_history.update_status_for(
                TaskHistory.STATUS_ERROR, details=error
            )
            return

        task_history.update_dbid(db=result['database'])
        task_history.update_status_for(
            TaskHistory.STATUS_SUCCESS, details='\nDatabase cloned successfully')

    except SoftTimeLimitExceeded:
        LOG.error("task id %s - timeout exceeded" % self.request.id)
        task_history.update_status_for(
            TaskHistory.STATUS_ERROR, details="timeout exceeded")
        if 'result' in locals() and result['created']:
            destroy_infra(
                databaseinfra=result['databaseinfra'], task=task_history)
            return
    except Exception as e:
        traceback = full_stack()
        LOG.error("Ops... something went wrong: %s" % e)
        LOG.error(traceback)

        if 'result' in locals() and result['created']:
            destroy_infra(
                databaseinfra=result['databaseinfra'], task=task_history)

        task_history.update_status_for(
            TaskHistory.STATUS_ERROR, details=traceback)

        return

    finally:
        AuditRequest.cleanup_request()


@app.task
@only_one(key="db_infra_notification_key", timeout=20)
def databaseinfra_notification(self, user=None):
    worker_name = get_worker_name()
    task_history = TaskHistory.register(
        request=self.request, user=user, worker_name=worker_name)
    threshold_infra_notification = Configuration.get_by_name_as_int(
        "threshold_infra_notification", default=0)
    if threshold_infra_notification <= 0:
        LOG.warning("database infra notification is disabled")
        return

    # Sum capacity per databseinfra with parameter plan, environment and engine
    infras = DatabaseInfra.objects.values(
        'plan__name', 'environment__name', 'engine__engine_type__name',
        'plan__provider'
    ).annotate(capacity=Sum('capacity'))
    for infra in infras:
        try:
            database = infra.databases.get()
        except Database.MultipleObjectsReturned:
            pass
        else:
            if database.is_in_quarantine:
                continue
            if not database.subscribe_to_email_events:
                continue

        used = DatabaseInfra.objects.filter(
            plan__name=infra['plan__name'], environment__name=infra['environment__name'],
            engine__engine_type__name=infra['engine__engine_type__name']
        ).aggregate(used=Count('databases'))
        # calculate the percentage

        percent = int(used['used'] * 100 / infra['capacity'])
        if percent >= threshold_infra_notification and infra['plan__provider'] != Plan.CLOUDSTACK:
            LOG.info('Plan %s in environment %s with %s%% occupied' % (
                infra['plan__name'], infra['environment__name'], percent))
            LOG.info("Sending database infra notification...")
            context = {}
            context['plan'] = infra['plan__name']
            context['environment'] = infra['environment__name']
            context['used'] = used['used']
            context['capacity'] = infra['capacity']
            context['percent'] = percent
            email_notifications.databaseinfra_ending(context=context)

        task_history.update_status_for(
            TaskHistory.STATUS_SUCCESS,
            details='Databaseinfra Notification successfully sent to dbaas admins!'
        )
    return


def database_notification_for_team(team=None):
    """
    Notifies teams of database usage.
    if threshold_database_notification <= 0, the notification is disabled.
    """
    LOG.info("sending database notification for team %s" % team)
    threshold_database_notification = Configuration.get_by_name_as_int(
        "threshold_database_notification", default=0)
    # if threshold_database_notification
    if threshold_database_notification <= 0:
        LOG.warning("database notification is disabled")
        return

    databases = Database.objects.filter(
        team=team, is_in_quarantine=False, subscribe_to_email_events=True
    )
    msgs = []
    for database in databases:
        used = database.used_size_in_mb
        capacity = database.total_size_in_mb
        try:
            percent_usage = (used / capacity) * 100
        except ZeroDivisionError:
            # database has no total size
            percent_usage = 0.0
        msg = "database %s => usage: %.2f | threshold: %.2f" % (
            database, percent_usage, threshold_database_notification)
        LOG.info(msg)
        msgs.append(msg)

        if not team.email:
            msgs.append(
                "team %s has no email set and therefore no database usage notification will been sent" % team)
        else:
            if percent_usage >= threshold_database_notification:
                LOG.info("Sending database notification...")
                context = {}
                context['database'] = database.name
                context['team'] = team
                context['measure_unity'] = "MB"
                context['used'] = used
                context['capacity'] = capacity
                context['percent'] = "%.2f" % percent_usage
                context['environment'] = database.environment.name
                email_notifications.database_usage(context=context)

    return msgs


@app.task(bind=True)
@only_one(key="db_notification_key", timeout=180)
def database_notification(self):
    """
    Create tasks for database notification by team
    if threshold_database_notification <= 0, the notification is disabled.
    """
    # get all teams and for each one create a new task
    LOG.info("retrieving all teams and sendind database notification")
    teams = Team.objects.all()
    msgs = {}

    for team in teams:
        ###############################################
        # create task
        ###############################################

        msgs[team] = database_notification_for_team(team=team)
        ###############################################

    try:
        LOG.info("Messages: ")
        LOG.info(msgs)

        worker_name = get_worker_name()
        task_history = TaskHistory.register(
            request=self.request, user=None, worker_name=worker_name)
        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details="\n".join(
            str(key) + ': ' + ', '.join(value) for key, value in msgs.items()))
    except Exception as e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)

    return


@app.task(bind=True)
@only_one(key="get_databases_status", timeout=180)
def update_database_status(self):
    LOG.info("Retrieving all databases")
    try:
        worker_name = get_worker_name()
        task_history = TaskHistory.register(
            request=self.request, user=None, worker_name=worker_name)
        databases = Database.objects.all()
        msgs = []
        for database in databases:
            if database.database_status.is_alive:
                database.status = Database.ALIVE

                instances_status = database.databaseinfra.check_instances_status()
                if instances_status == database.databaseinfra.ALERT:
                    database.status = Database.ALERT

            else:
                database.status = Database.DEAD

            database.save(update_fields=['status'])
            msg = "\nUpdating status for database: {}, status: {}".format(
                database, database.status)
            msgs.append(msg)
            LOG.info(msg)

        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details="\n".join(
            value for value in msgs))
    except Exception as e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)

    return


@app.task(bind=True)
@only_one(key="get_databases_used_size", timeout=180)
def update_database_used_size(self):
    LOG.info("Retrieving all databases")
    try:
        worker_name = get_worker_name()
        task_history = TaskHistory.register(
            request=self.request, user=None, worker_name=worker_name)
        databases = Database.objects.all()
        msgs = []
        for database in databases:
            if database.database_status:
                database.used_size_in_bytes = float(
                    database.database_status.used_size_in_bytes)
            else:
                database.used_size_in_bytes = 0.0

            database.save(update_fields=['used_size_in_bytes'])
            msg = "\nUpdating used size in bytes for database: {}, used size: {}".format(
                database, database.used_size_in_bytes)
            msgs.append(msg)
            LOG.info(msg)

        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details="\n".join(
            value for value in msgs))
    except Exception as e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)

    return


@app.task(bind=True)
@only_one(key="get_instances_status", timeout=180)
def update_instances_status(self):
    LOG.info("Retrieving all databaseinfras")
    worker_name = get_worker_name()
    task_history = TaskHistory.register(
        request=self.request, user=None, worker_name=worker_name)

    try:
        infras = DatabaseInfra.objects.all()
        msgs = []
        for databaseinfra in infras:
            LOG.info("Retrieving all instances for {}".format(databaseinfra))

            for instance in Instance.objects.filter(databaseinfra=databaseinfra, is_arbiter=False):
                if instance.check_status():
                    instance.status = Instance.ALIVE
                else:
                    instance.status = Instance.DEAD

                instance.save(update_fields=['status'])

                msg = "\nUpdating instance status, instance: {}, status: {}".format(
                    instance, instance.status)
                msgs.append(msg)
                LOG.info(msg)

        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details="\n".join(
            value for value in msgs))
    except Exception as e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)

    return


@app.task(bind=True)
@only_one(key="purge_task_history", timeout=600)
def purge_task_history(self):
    try:
        worker_name = get_worker_name()
        task_history = TaskHistory.register(
            request=self.request, user=None, worker_name=worker_name)

        now = datetime.datetime.now()
        retention_days = Configuration.get_by_name_as_int(
            'task_history_retention_days')

        n_days_before = now - datetime.timedelta(days=retention_days)

        tasks_to_purge = TaskHistory.objects.filter(
            task_name__in=[
                'notification.tasks.database_notification',
                'notification.tasks.database_notification_for_team',
                'notification.tasks.update_database_used_size',
                'notification.tasks.update_disk_used_size',
                'notification.tasks.update_database_status',
                'notification.tasks.update_instances_status',
                'system.tasks.set_celery_healthcheck_last_update'
            ],
            ended_at__lt=n_days_before,
            task_status__in=["SUCCESS", "ERROR", "WARNING"])

        tasks_to_purge.delete()

        task_history.update_status_for(TaskHistory.STATUS_SUCCESS,
                                       details='Purge succesfully done!')
    except Exception as e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)


@app.task(bind=True)
def resize_database(self, database, cloudstackpack, task_history=None, user=None):
    AuditRequest.new_request("resize_database", user, "localhost")

    try:
        worker_name = get_worker_name()
        task_history = TaskHistory.register(request=self.request, task_history=task_history,
                                            user=user, worker_name=worker_name)
        from util.providers import resize_database_instances
        from util import get_credentials_for
        from dbaas_cloudstack.provider import CloudStackProvider
        from dbaas_credentials.models import CredentialType

        cs_credentials = get_credentials_for(environment=database.environment,
                                             credential_type=CredentialType.CLOUDSTACK)
        cs_provider = CloudStackProvider(credentials=cs_credentials)

        databaseinfra = database.databaseinfra
        driver = databaseinfra.get_driver()
        instances = driver.get_slave_instances()
        instances.append(driver.get_master_instance())
        instances_to_resize = []
        resized_instances = []

        disable_zabbix_alarms(database)

        for instance in instances:
            host = instance.hostname
            host_attr = host.cs_host_attributes.get()
            offering_id = cs_provider.get_vm_offering_id(
                vm_id=host_attr.vm_id,
                project_id=cs_credentials.project
            )

            if offering_id == cloudstackpack.offering.serviceofferingid:
                LOG.info("Instance offering: {}".format(offering_id))
                resized_instances.append(instance)
            else:
                instances_to_resize.append(instance)

        result = resize_database_instances(
            database=database,
            cloudstackpack=cloudstackpack,
            instances=instances_to_resize,
            task=task_history
        )

        if result['created']:
            resized_instances += result['completed_instances']
        else:
            if 'exceptions' not in result:
                error = "Something went wrong."
            else:
                error = "\n".join(
                    ": ".join(err) for err in
                    result['exceptions']['error_codes']
                )
                traceback = "\nException Traceback\n".join(
                    result['exceptions']['traceback']
                )

                error = "{}\n{}\n{}".format(error, traceback, error)

        if databaseinfra.plan.is_ha:
            LOG.info("Waiting 60s to check continue...")
            sleep(60)
            instance = driver.get_slave_instances()[0]
            driver.check_replication_and_switch(instance)

        if len(instances) == len(resized_instances):
            from dbaas_cloudstack.models import DatabaseInfraOffering
            LOG.info('Updating offering DatabaseInfra.')

            databaseinfraoffering = DatabaseInfraOffering.objects.get(
                databaseinfra=databaseinfra)
            databaseinfraoffering.offering = cloudstackpack.offering
            databaseinfraoffering.save()

            if databaseinfra.engine.engine_type.name == 'redis':
                new_max_memory = databaseinfraoffering.offering.memory_size_mb
                resize_factor = 0.5
                if new_max_memory > 1024:
                    resize_factor = 0.75

                new_max_memory *= resize_factor
                databaseinfra.per_database_size_mbytes = int(new_max_memory)
                databaseinfra.save()

            task_history.update_status_for(TaskHistory.STATUS_SUCCESS,
                                           details='Resize successfully done.')
            return

        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=error)
        return

    except Exception as e:
        error = "Resize Database ERROR: {}".format(e)
        LOG.error(error)
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=error)

    finally:
        enable_zabbix_alarms(database)
        AuditRequest.cleanup_request()


def disable_zabbix_alarms(database):
    LOG.info("{} alarms will be disabled!".format(database))
    zabbix_provider = handle_zabbix_alarms(database)
    zabbix_provider.disable_alarms()


def enable_zabbix_alarms(database):
    LOG.info("{} alarms will be enabled!".format(database))
    zabbix_provider = handle_zabbix_alarms(database)
    zabbix_provider.enable_alarms()


def create_zabbix_alarms(database):
    LOG.info("{} alarms will be created!".format(database))
    zabbix_provider = handle_zabbix_alarms(database)
    zabbix_provider.create_basic_monitors()
    zabbix_provider.create_database_monitors()


def delete_zabbix_alarms(database):
    LOG.info("{} alarms will be deleted!".format(database))
    zabbix_provider = handle_zabbix_alarms(database)
    zabbix_provider.delete_basic_monitors()
    zabbix_provider.delete_database_monitors()


def handle_zabbix_alarms(database):
    from dbaas_zabbix import factory_for
    from dbaas_credentials.credential import Credential
    from dbaas_credentials.models import CredentialType
    integration = CredentialType.objects.get(type=CredentialType.ZABBIX)
    credentials = Credential.get_credentials(environment=database.databaseinfra.environment,
                                             integration=integration)

    return factory_for(databaseinfra=database.databaseinfra, credentials=credentials)


@app.task(bind=True)
def upgrade_mongodb_24_to_30(self, database, user, task_history=None):

    from workflow.settings import MONGODB_UPGRADE_24_TO_30_SINGLE
    from workflow.settings import MONGODB_UPGRADE_24_TO_30_HA
    from util import build_dict
    from workflow.workflow import start_workflow

    worker_name = get_worker_name()
    task_history = TaskHistory.register(request=self.request, task_history=task_history,
                                        user=user, worker_name=worker_name)

    databaseinfra = database.databaseinfra
    driver = databaseinfra.get_driver()

    instances = driver.get_database_instances()
    source_plan = databaseinfra.plan
    target_plan = source_plan.engine_equivalent_plan

    source_engine = databaseinfra.engine
    target_engine = source_engine.engine_upgrade_option

    if source_plan.is_ha:
        steps = MONGODB_UPGRADE_24_TO_30_HA
    else:
        steps = MONGODB_UPGRADE_24_TO_30_SINGLE

    stop_now = False

    if not target_plan:
        msg = "There is not Engine Equivalent Plan!"
        stop_now = True

    if not target_engine:
        msg = "There is not Engine Upgrade Option!"
        stop_now = True

    if database.status != Database.ALIVE or not database.database_status.is_alive:
        msg = "Database is not alive!"
        stop_now = True

    if database.is_beeing_used_elsewhere(task_id=self.request.id):
        msg = "Database is in use by another task!"
        stop_now = True

    if not source_engine.version.startswith('2.4.'):
        msg = "Database version must be 2.4!"
        stop_now = True

    if target_engine and target_engine.version != '3.0.12':
        msg = "Target database version must be 3.0.12!"
        stop_now = True

    if stop_now:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=msg)
        LOG.info("Upgrade finished")
        return

    try:
        delete_zabbix_alarms(database)
    except Exception as e:
        message = "Could not delete Zabbix alarms: {}".format(e)
        task_history.update_status_for(
            TaskHistory.STATUS_ERROR, details=message
        )
        LOG.error(message)
        return

    try:
        workflow_dict = build_dict(steps=steps,
                                   databaseinfra=databaseinfra,
                                   instances=instances,
                                   source_plan=source_plan,
                                   target_plan=target_plan,
                                   source_engine=source_engine,
                                   target_engine=target_engine)

        start_workflow(workflow_dict=workflow_dict, task=task_history)

        if workflow_dict['exceptions']['traceback']:
            error = "\n".join(": ".join(err) for err in workflow_dict['exceptions']['error_codes'])
            traceback = "\nException Traceback\n".join(workflow_dict['exceptions']['traceback'])
            error = "{}\n{}\n{}".format(error, traceback, error)
            task_history.update_status_for(TaskHistory.STATUS_ERROR, details=error)
            LOG.info("MongoDB Upgrade finished with errors")
            return

        task_history.update_status_for(
            TaskHistory.STATUS_SUCCESS, details='MongoDB sucessfully upgraded!')

        LOG.info("MongoDB Upgrade finished")
    except Exception as e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)
        LOG.warning("MongoDB Upgrade finished with errors")

    try:
        create_zabbix_alarms(database)
    except Exception as e:
        message = "Could not create Zabbix alarms: {}".format(e)
        task_history.update_status_for(
            TaskHistory.STATUS_ERROR, details=message
        )
        LOG.error(message)


@app.task(bind=True)
def database_disk_resize(self, database, disk_offering, task_history, user):
    from dbaas_nfsaas.models import HostAttr
    from workflow.steps.util.nfsaas_utils import resize_disk

    AuditRequest.new_request("database_disk_resize", user, "localhost")

    databaseinfra = database.databaseinfra
    old_disk_offering = database.databaseinfra.disk_offering
    resized = []

    try:
        worker_name = get_worker_name()
        task_history = TaskHistory.register(
            request=self.request, task_history=task_history,
            user=user, worker_name=worker_name
        )

        task_history.update_details(
            persist=True,
            details='\nLoading Disk offering'
        )

        for instance in databaseinfra.get_driver().get_database_instances():
            if not HostAttr.objects.filter(host_id=instance.hostname_id).exists():
                continue

            task_history.update_details(
                persist=True,
                details='\nChanging instance {} to '
                        'NFS {}'.format(instance, disk_offering)
            )
            if resize_disk(
                    environment=database.environment,
                    host=instance.hostname,
                    disk_offering=disk_offering):
                resized.append(instance)

        task_history.update_details(
            persist=True,
            details='\nUpdate DBaaS metadata from {} to '
                    '{}'.format(databaseinfra.disk_offering, disk_offering)
        )
        databaseinfra.disk_offering = disk_offering
        databaseinfra.save()

        task_history.update_status_for(
            status=TaskHistory.STATUS_SUCCESS,
            details='\nDisk resize successfully done.'
        )
        return True

    except Exception as e:
        error = "Disk resize ERROR: {}".format(e)
        LOG.error(error)

        if databaseinfra.disk_offering != old_disk_offering:
            task_history.update_details(
                persist=True, details='\nUndo update DBaaS metadata'
            )
            databaseinfra.disk_offering = old_disk_offering
            databaseinfra.save()

        for instance in resized:
            task_history.update_details(
                persist=True,
                details='\nUndo NFS change for instance {}'.format(instance)
            )
            resize_disk(
                environment=database.environment,
                host=instance.hostname,
                disk_offering=old_disk_offering
            )

        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=error)
    finally:
        AuditRequest.cleanup_request()


@app.task(bind=True)
@only_one(key="disk_auto_resize", timeout=600)
def update_disk_used_size(self):
    worker_name = get_worker_name()
    task = TaskHistory.register(
        request=self.request, user=None, worker_name=worker_name
    )
    task.add_detail(message='Collecting disk used space from Zabbix')

    from .tasks_disk_resize import zabbix_collect_used_disk
    zabbix_collect_used_disk(task=task)


@app.task(bind=True)
def upgrade_database(self, database, user, task):
    worker_name = get_worker_name()
    task = TaskHistory.register(self.request, user, task, worker_name)

    source_plan = database.infra.plan
    target_plan = source_plan.engine_equivalent_plan

    class_path = target_plan.replication_topology.class_path
    steps = get_database_upgrade_setting(class_path)

    success = steps_for_instances(steps, database.infra.instances.all(), task)

    if success:
        task.update_status_for(TaskHistory.STATUS_SUCCESS, 'Done')
    else:
        task.update_status_for(
            TaskHistory.STATUS_ERROR,
            'Could not do upgrade.\nUpgrade don\'t has rollback'
        )


def steps_for_instances(steps, instances, task):
    steps_total = len(steps) * len(instances)
    step_current = 0

    for instance in instances:
        task.add_detail('Instance: {}'.format(instance))
        for step in steps:
            step_current += 1

            try:
                step_class = import_by_path(step)
                step_instance = step_class(instance)

                task.add_step(step_current, steps_total, str(step_instance))

                step_instance.do()
            except Exception as e:
                task.add_detail(str(e))
                task.add_detail(full_stack())
                return False

    return True
