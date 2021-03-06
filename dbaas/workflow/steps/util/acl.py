# -*- coding: utf-8 -*-
from workflow.steps.util.base import BaseInstanceStep
from dbaas_aclapi.tasks import replicate_acl_for
from dbaas_aclapi.acl_base_client import AclClient
from dbaas_aclapi import helpers
from dbaas_aclapi.models import DatabaseInfraInstanceBind
from dbaas_aclapi.models import ERROR
from dbaas_credentials.models import CredentialType
from util import get_credentials_for

import logging

LOG = logging.getLogger(__name__)


class ACLStep(BaseInstanceStep):

    def __init__(self, instance):
        super(ACLStep, self).__init__(instance)
        self.databaseinfra = self.instance.databaseinfra
        self.environment = self.databaseinfra.environment
        self.database = self.databaseinfra.databases.first()

        acl_credential = get_credentials_for(
            environment=self.environment,
            credential_type=CredentialType.ACLAPI)
        self.acl_client = AclClient(
            acl_credential.endpoint,
            acl_credential.user,
            acl_credential.password,
            self.environment)

    def do(self):
        raise NotImplementedError

    def undo(self):
        pass


class ReplicateAcls2NewInstance(ACLStep):

    def __unicode__(self):
        return "Replicating acls ..."

    def do(self):
        source_instance = self.databaseinfra.instances.filter(
            is_active=True,
            read_only=False
        ).first()
        replicate_acl_for(
            database=self.database,
            old_ip=source_instance.address,
            new_ip=self.instance.address)


class BindNewInstance(ACLStep):

    def __unicode__(self):
        return "Binding new instance ..."

    def __init__(self, instance):
        super(BindNewInstance, self).__init__(instance)
        self.instances = [self.instance]
        self.instance_address_list = [self.instance.address]

    def do(self):
        for database_bind in self.database.acl_binds.all():
            if helpers.bind_address(database_bind=database_bind,
                                    acl_client=self.acl_client,
                                    instances=self.instances,
                                    infra_attr_instances=[],
                                    infra_vips=[]):
                continue
            else:
                LOG.error("The AclApi is not working properly.")
                database_bind.bind_status = ERROR
                database_bind.save()
                DatabaseInfraInstanceBind.objects.filter(
                    databaseinfra=self.databaseinfra,
                    bind_address=database_bind.bind_address,
                    instance__in=self.instance_address_list
                ).update(bind_status=ERROR)

    def undo(self):
        for database_bind in self.database.acl_binds.all():
            infra_instances_binds = DatabaseInfraInstanceBind.objects.filter(
                databaseinfra=self.databaseinfra,
                bind_address=database_bind.bind_address,
                instance__in=self.instance_address_list,
            )
            helpers.unbind_address(
                database_bind=database_bind,
                acl_client=self.acl_client,
                infra_instances_binds=infra_instances_binds,
                delete_database_bind=False
            )
