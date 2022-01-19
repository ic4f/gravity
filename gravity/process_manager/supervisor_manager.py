"""
"""
import errno
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from os.path import exists, join

import click

from gravity.io import debug, error, info, warn
from gravity.process_manager import BaseProcessManager

from supervisor import supervisorctl


SUPERVISORD_CONF_TEMPLATE = """;
; This file is maintained by Galaxy - CHANGES WILL BE OVERWRITTEN
;

[unix_http_server]
file = {supervisor_state_dir}/supervisor.sock

[supervisord]
logfile = {supervisor_state_dir}/supervisord.log
pidfile = {supervisor_state_dir}/supervisord.pid
loglevel = info
nodaemon = false

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl = unix://{supervisor_state_dir}/supervisor.sock

[include]
files = {supervisord_conf_dir}/*.d/*.conf {supervisord_conf_dir}/*.conf
"""

# TODO: with more templating you only need one of these
SUPERVISORD_SERVICE_TEMPLATES = {}
SUPERVISORD_SERVICE_TEMPLATES["gunicorn"] = """;
; This file is maintained by Galaxy - CHANGES WILL BE OVERWRITTEN
;

[program:{program_name}]
command         = {command}
directory       = {galaxy_root}
umask           = {galaxy_umask}
autostart       = true
autorestart     = true
startsecs       = 15
stopwaitsecs    = 65
environment     = GALAXY_CONFIG_FILE="{galaxy_conf}"
numprocs        = 1
stdout_logfile  = {log_file}
redirect_stderr = true
{process_name_opt}
"""  # noqa: E501

SUPERVISORD_SERVICE_TEMPLATES["celery"] = """;
; This file is maintained by Galaxy - CHANGES WILL BE OVERWRITTEN
;

[program:{program_name}]
command         = {command}
directory       = {galaxy_root}
umask           = {galaxy_umask}
autostart       = true
autorestart     = true
startsecs       = 10
stopwaitsecs    = 10
environment     = PYTHONPATH=lib,GALAXY_CONFIG_FILE="{galaxy_conf}"
numprocs        = 1
stdout_logfile  = {log_file}
redirect_stderr = true
{process_name_opt}
"""

SUPERVISORD_SERVICE_TEMPLATES["celery-beat"] = """;
; This file is maintained by Galaxy - CHANGES WILL BE OVERWRITTEN
;

[program:{program_name}]
command         = {command}
directory       = {galaxy_root}
umask           = {galaxy_umask}
autostart       = true
autorestart     = true
startsecs       = 10
stopwaitsecs    = 10
environment     = PYTHONPATH=lib,GALAXY_CONFIG_FILE="{galaxy_conf}"
numprocs        = 1
stdout_logfile  = {log_file}
redirect_stderr = true
{process_name_opt}
"""

SUPERVISORD_SERVICE_TEMPLATES["standalone"] = """;
; This file is maintained by Galaxy - CHANGES WILL BE OVERWRITTEN
;

[program:{program_name}]
command         = {command}
directory       = {galaxy_root}
autostart       = true
autorestart     = true
startsecs       = 20
stopwaitsecs    = 65
numprocs        = 1
stdout_logfile  = {log_file}
redirect_stderr = true
{process_name_opt}
"""

SUPERVISORD_GROUP_TEMPLATE = """;
; This file is maintained by Galaxy - CHANGES WILL BE OVERWRITTEN
;

[group:{instance_name}]
programs = {programs}
"""


def which(file):
    # http://stackoverflow.com/questions/5226958/which-equivalent-function-in-python
    if os.path.exists(os.path.dirname(sys.executable) + "/" + file):
        return os.path.dirname(sys.executable) + "/" + file
    for path in os.environ["PATH"].split(":"):
        if os.path.exists(path + "/" + file):
            return path + "/" + file

    return None


class SupervisorProcessManager(BaseProcessManager):
    def __init__(self, state_dir=None, start_daemon=True, foreground=False):
        super(SupervisorProcessManager, self).__init__(state_dir=state_dir)
        self.supervisord_exe = which("supervisord")
        self.supervisor_state_dir = join(self.state_dir, "supervisor")
        self.supervisord_conf_path = join(self.supervisor_state_dir, "supervisord.conf")
        self.supervisord_conf_dir = join(self.supervisor_state_dir, "supervisord.conf.d")
        self.supervisord_pid_path = join(self.supervisor_state_dir, "supervisord.pid")
        self.supervisord_sock_path = join(self.supervisor_state_dir, "supervisor.sock")
        self.__supervisord_popen = None
        self.use_group = not self.config_manager.single_instance
        self.foreground = foreground
        self.tail = which("tail")

        if not exists(self.supervisord_conf_dir):
            os.makedirs(self.supervisord_conf_dir)

        if start_daemon:
            self.__supervisord()

    def __supervisord_is_running(self):
        try:
            assert exists(self.supervisord_pid_path)
            assert exists(self.supervisord_sock_path)
            os.kill(int(open(self.supervisord_pid_path).read()), 0)
            return True
        except Exception:
            return False

    def __supervisord(self):
        format_vars = {"supervisor_state_dir": self.supervisor_state_dir, "supervisord_conf_dir": self.supervisord_conf_dir}
        supervisord_cmd = [self.supervisord_exe, "-c", self.supervisord_conf_path]
        if self.foreground:
            supervisord_cmd.append('--nodaemon')
        if not self.__supervisord_is_running():
            # any time that supervisord is not running, let's rewrite supervisord.conf
            open(self.supervisord_conf_path, "w").write(SUPERVISORD_CONF_TEMPLATE.format(**format_vars))
            self.__supervisord_popen = subprocess.Popen(supervisord_cmd, env=os.environ)
            rc = self.__supervisord_popen.poll()
            if rc:
                error("supervisord exited with code %d" % rc)
            # FIXME: don't wait forever
            while not exists(self.supervisord_pid_path) or not exists(self.supervisord_sock_path):
                debug(f"Waiting for {self.supervisord_pid_path}")
                time.sleep(0.5)

    def __get_supervisor(self):
        """Return the supervisor proxy object

        Should probably use this more rather than supervisorctl directly
        """
        options = supervisorctl.ClientOptions()
        options.realize(args=["-c", self.supervisord_conf_path])
        return supervisorctl.Controller(options).get_supervisor()

    def terminate(self):
        if self.foreground:
            # if running in foreground, if terminate is called, then supervisord should've already received a SIGINT
            self.__supervisord_popen.wait()

    def __service_program_name(self, instance_name, service):
        if self.use_group:
            return f"{instance_name}_{service['config_type']}_{service['service_type']}_{service['service_name']}"
        else:
            return service["service_name"]

    def __service_log_file(self, log_dir, program_name):
        return join(log_dir, program_name + ".log")

    def __update_service(self, config_file, config, attribs, service, instance_conf_dir, instance_name):
        if self.use_group:
            process_name_opt = f"process_name    = {service['service_name']}"
        else:
            process_name_opt = ""

        program_name = self.__service_program_name(instance_name, service)

        # used by the "standalone" service type
        attach_to_pool_opt = ""
        server_pool = service.get("server_pool")
        if server_pool:
            attach_to_pool_opt = f" --attach-to-pool={server_pool}"

        format_vars = {
            "log_file": self.__service_log_file(attribs["log_dir"], program_name),
            "config_type": service["config_type"],
            "server_name": service["service_name"],
            "attach_to_pool_opt": attach_to_pool_opt,
            "bind_address": service.get("bind_address"),
            "bind_port": service.get("bind_port"),
            "galaxy_umask": service.get("umask", "022"),
            "program_name": program_name,
            "process_name_opt": process_name_opt,
            "galaxy_conf": config_file,
            "galaxy_root": attribs["galaxy_root"],
            "supervisor_state_dir": self.supervisor_state_dir,
        }
        format_vars["command"] = service.command_template.format(**format_vars)
        conf = join(instance_conf_dir, f"{service['config_type']}_{service['service_type']}_{service['service_name']}.conf")

        if not exists(attribs["log_dir"]):
            os.makedirs(attribs["log_dir"])

        template = SUPERVISORD_SERVICE_TEMPLATES.get(service["service_type"])
        if not template:
            raise Exception(f"Unknown service type: {service['service_type']}")

        with open(conf, "w") as out:
            out.write(template.format(**format_vars))

    def _process_config_changes(self, configs, meta_changes):
        # remove the services of any configs which have been removed
        for config in meta_changes["remove_configs"].values():
            instance_name = config.instance_name
            instance_conf_dir = join(self.supervisord_conf_dir, f"{instance_name}.d")
            for service in config["services"]:
                info("Removing service %s", self.__service_program_name(instance_name, service))
                conf = join(instance_conf_dir, f"{service.config_type}_{service.service_type}_{service.service_name}.conf")
                if exists(conf):
                    os.unlink(conf)

        # update things for existing or new configs
        for config_file, config in configs.items():
            instance_name = config["instance_name"]
            attribs = config["attribs"]
            update_all_configs = False

            # config attribs have changed (galaxy_root, virtualenv, etc.)
            if "update_attribs" in config:
                info(f"Updating all dependent services of config {config_file} due to changes")
                attribs = config["update_attribs"]
                update_all_configs = True

            # instance name has changed, so supervisor group config must change
            if "update_instance_name" in config:
                instance_name = config["update_instance_name"]
                info("Creating new instance for name change: %s -> %s", config["instance_name"], instance_name)
                update_all_configs = True

            # always attempt to make the config dir
            instance_conf_dir = join(self.supervisord_conf_dir, f"{instance_name}.d")
            try:
                os.makedirs(instance_conf_dir)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise

            if update_all_configs:
                for service in config["services"]:
                    info("Updating service %s:%s_%s_%s", self.__service_program_name(instance_name, service))
                    self.__update_service(config_file, config, attribs, service, instance_conf_dir, instance_name)

            # new services
            if "update_services" in config:
                for service in config["update_services"]:
                    info("Creating or updating service %s", self.__service_program_name(instance_name, service))
                    self.__update_service(config_file, config, attribs, service, instance_conf_dir, instance_name)

            # deleted services
            if "remove_services" in config:
                for service in config["remove_services"]:
                    info("Removing service %s:%s_%s_%s", self.__service_program_name(instance_name, service))
                    conf = join(instance_conf_dir, f"{service['config_type']}_{service['service_type']}_{service['service_name']}.conf")
                    if exists(conf):
                        os.unlink(conf)

            # sanity check, make sure everything that should exist does exist
            for service in config["services"]:
                conf = join(instance_conf_dir, f"{service['config_type']}_{service['service_type']}_{service['service_name']}.conf")
                if service not in config.get("remove_services", []) and not exists(conf):
                    self.__update_service(config_file, config, attribs, service, instance_conf_dir, instance_name)
                    warn(f"Missing service config recreated: {conf}")

        # all configs referencing an instance name have been removed (or their
        # instance names have changed), nuke the group
        for instance_name in meta_changes["remove_instances"]:
            info("Removing instance %s", instance_name)
            instance_conf_dir = join(self.supervisord_conf_dir, f"{instance_name}.d")
            if exists(instance_conf_dir):
                shutil.rmtree(instance_conf_dir)
            conf = join(self.supervisord_conf_dir, f"group_{instance_name}.conf")
            if exists(conf):
                os.unlink(join(conf))

        # persist to the state file
        self.config_manager.register_config_changes(configs, meta_changes)

        # now we can create/update the instance group
        for instance_name in meta_changes["changed_instances"]:
            programs = []
            for service in self.config_manager.get_registered_services():
                if service["instance_name"] == instance_name and service["service_type"] != "uwsgi":
                    programs.append(f"{instance_name}_{service['config_type']}_{service['service_type']}_{service['service_name']}")
            conf = join(self.supervisord_conf_dir, f"group_{instance_name}.conf")
            if programs and self.use_group:
                format_vars = {"instance_conf_dir": instance_conf_dir, "instance_name": instance_name, "programs": ",".join(programs)}
                open(conf, "w").write(SUPERVISORD_GROUP_TEMPLATE.format(**format_vars))
            else:
                # no programs for the group, so it should be removed
                if exists(conf):
                    os.unlink(conf)

    def __start_stop(self, op, instance_names):
        self.update()
        instance_names, unknown_instance_names = self.get_instance_names(instance_names)
        for instance_name in instance_names:
            target = f"{instance_name}:*" if self.use_group else "all"
            self.supervisorctl(op, target)
            for service in self.config_manager.get_instance_services(instance_name):
                if service["service_type"] == "uwsgi":
                    self.supervisorctl(op, f"{instance_name}_{service['config_type']}_{service['service_name']}")
        # shortcut for just passing service names directly
        for name in unknown_instance_names:
            self.supervisorctl(op, name)

    def __reload_graceful(self, op, instance_names):
        self.update()
        for instance_name in self.get_instance_names(instance_names)[0]:
            if op == "reload":
                # restart everything but uwsgi
                target = f"{instance_name}:*" if self.use_group else "all"
                self.supervisorctl("restart", target)
            for service in self.config_manager.get_instance_services(instance_name):
                service_name = f"{instance_name}_{service.config_type}_{service.service_name}"
                group_service_name = f"{instance_name}:{service.config_type}_{service.service_name}"
                if service["service_type"] == "uwsgi":
                    procinfo = self.__get_supervisor().getProcessInfo(service_name)
                    # restart uwsgi
                    try:
                        os.kill(procinfo["pid"], signal.SIGHUP)
                        click.echo(f"{group_service_name}: sent HUP signal")
                    except Exception as exc:
                        warn("Attempt to reload %s failed: %s", service_name, exc)
                # graceful restarts
                elif op == "graceful" and service["service_type"] == "standalone":
                    self.supervisorctl("restart", group_service_name)
                elif op == "graceful" and service["service_type"] == "paste":
                    self.supervisorctl("restart", group_service_name)
                    url = "http://localhost:%d/" % service.paste_port
                    click.echo(f"{service_name}: waiting until {url} is accepting requests", end="")
                    while True:
                        try:
                            r = urllib.request.urlopen(url, None, 5)
                            assert r.getcode() == 200, f"{url} returned HTTP code: {r.getcode()}"
                            click.echo(" OK")
                            break
                        except AssertionError as exc:
                            click.echo()
                            error(exc)
                            return
                        except Exception:
                            click.echo(".", nl=False)
                            sys.stdout.flush()
                            time.sleep(1)

    def start(self, instance_names):
        super(SupervisorProcessManager, self).start(instance_names)
        self.__start_stop("start", instance_names)
        self.supervisorctl("status")

    def stop(self, instance_names):
        self.__start_stop("stop", instance_names)
        # Exit supervisor if all processes are stopped
        supervisor = self.__get_supervisor()
        proc_infos = supervisor.getAllProcessInfo()
        if all([i["state"] == 0 for i in proc_infos]):
            info("All processes stopped, supervisord will exit")
            self.shutdown()
        else:
            info("Not all processes stopped, supervisord not shut down (hint: see `galaxyctl status`)")

    def restart(self, instance_names):
        self.__start_stop("restart", instance_names)

    def reload(self, instance_names):
        self.__reload_graceful("reload", instance_names)

    def graceful(self, instance_names):
        self.__reload_graceful("graceful", instance_names)

    def status(self):
        # TODO: create our own formatted output
        # supervisor = self.get_supervisor()
        # all_infos = supervisor.getAllProcessInfo()
        self.supervisorctl("status")

    # FIXME: service_name is probably not right here
    def follow(self, instance_names):
        if not instance_names:
            instance_names = self.get_instance_names(instance_names)[0]
        if len(instance_names) != 1:
            error(f"Can only follow logs of one instance at a time! {instance_names}")
            return
        instance_name = instance_names[0]
        if self.tail:
            services = self.config_manager.get_instance_services(instance_name)
            config = self.config_manager.get_instance_config(instance_name)
            log_dir = config["attribs"]["log_dir"]
            log_files = []
            for service in services:
                program_name = self.__service_program_name(instance_name, service)
                log_files.append(self.__service_log_file(log_dir, program_name))
            cmd = [self.tail, "-f"] + log_files
            tail_popen = subprocess.Popen(cmd)
            tail_popen.wait()
        else:
            self.supervisorctl("tail", "-f", service_name)

    def shutdown(self):
        self.supervisorctl("shutdown")

    def update(self):
        """Add newly defined servers, remove any that are no longer present"""
        configs, meta_changes = self.config_manager.determine_config_changes()
        self._process_config_changes(configs, meta_changes)
        # only need to update if supervisord is running, otherwise changes will be picked up at next start
        if self.__supervisord_is_running():
            self.supervisorctl("update")

    def supervisorctl(self, *args, **kwargs):
        if not self.__supervisord_is_running():
            warn("supervisord is not running")
            return
        try:
            supervisorctl.main(args=["-c", self.supervisord_conf_path] + list(args))
        except SystemExit as e:
            # supervisorctl.main calls sys.exit(), so we catch that
            if e.code == 0:
                pass
            else:
                raise
