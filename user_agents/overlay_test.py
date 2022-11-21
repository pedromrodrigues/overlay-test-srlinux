#!/usr/bin/env python
# coding=utf-8
import grpc

from datetime import datetime
import time
import sys
import logging
import json
import socket
import subprocess
import re
import threading
import traceback
import sdk_service_pb2
import sdk_service_pb2_grpc
import config_service_pb2
import sdk_common_pb2
# To report state back
import telemetry_service_pb2
import telemetry_service_pb2_grpc
from logging.handlers import RotatingFileHandler
import netns
import signal
import os
from gnmi_calls import disable_admin_state, get_number_of_tests

################
## Agent name ##
################
agent_name='overlay_test'

########################################################
## Open a GRPC channel to connect to sdk_mgr on the dut
## sdk_mgr will be listening on 50053
########################################################
channel = grpc.insecure_channel('127.0.0.1:50053')
metadata = [('agent_name', agent_name)]
stub = sdk_service_pb2_grpc.SdkMgrServiceStub(channel)
thread_exit = False
threads = []

from enum import Enum
class Existence(Enum):
    NEW = 1
    ALREADY_EXISTED = 2

##################################################################
## State                                                        ##
## Contains the admin_state and the dict of each target ip/fqdn ##
##################################################################
class State(object):
    def __init__(self):
        self.admin_state = ''
        self.targets = {}
    
    def __str__(self):
        return str(self.__class__) + ": " + str(self.__dict__)

#################################
## Subscribe to required event ##
#################################
def Subscribe(stream_id, option):

    op = sdk_service_pb2.NotificationRegisterRequest.AddSubscription

    if option == 'cfg':
        entry = config_service_pb2.ConfigSubscriptionRequest()
        request = sdk_service_pb2.NotificationRegisterRequest(op=op, stream_id=stream_id, config=entry)

    subscription_response = stub.NotificationRegister(request=request, metadata=metadata)
    logger.info( f'Status of subscription response for {option}:: {subscription_response.status}' )

##################################################
## Subscribe to all the events that Agent needs ##
##################################################
def Subscribe_Notifications(stream_id):

    if not stream_id:
        logger.info("Stream ID not sent.")
        return False
    
    Subscribe(stream_id, 'cfg')

#####################################################
## Add data to the agent's state through Telemetry ##
#####################################################
def Add_Telemetry(path_obj_list):
    telemetry_stub = telemetry_service_pb2_grpc.SdkMgrTelemetryServiceStub(channel)
    telemetry_update_request = telemetry_service_pb2.TelemetryUpdateRequest()
    for js_path, obj in path_obj_list:
        telemetry_info = telemetry_update_request.state.add()
        telemetry_info.key.js_path = js_path
        telemetry_info.data.json_content = json.dumps(obj)
    logger.info(f"Telemetry_update_request :: {telemetry_update_request}")
    telemetry_response = telemetry_stub.TelemetryAddOrUpdate(request=telemetry_update_request, metadata=metadata)
    return telemetry_response

#####################################################
## Add data to the agent's state through Telemetry ##
#####################################################
def Remove_Telemetry(js_paths):
    telemetry_stub = telemetry_service_pb2_grpc.SdkMgrTelemetryServiceStub(channel)
    telemetry_del_request = telemetry_service_pb2.TelemetryDeleteRequest()
    for path in js_paths:
        telemetry_key = telemetry_del_request.key.add()
        telemetry_key.js_path = path
    logger.info(f"Telemetry_Delete_Request :: {telemetry_del_request}")
    telemetry_response = telemetry_stub.TelemetryDelete(request=telemetry_del_request, metadata=metadata)
    return telemetry_response

def send_keep_alive():
    global thread_exit

    while not thread_exit:
        keep_alive_response = stub.KeepAlive(request=sdk_service_pb2.KeepAliveRequest(),metadata=metadata)

        if keep_alive_response == sdk_common_pb2.SdkMgrStatus.Value("kSdkMgrFailed"):
            logger.info("Fail")
        time.sleep(3)

###################################################################
## Each Thread corresponds to a Test for a specific IP/FQDN      ##
## It runs a specific test and adds its results to agent's state ##
###################################################################
from threading import Thread
from threading import Event
class ServiceMonitoringThread(Thread):
    def __init__(self,network_instance,destination,number_of_tests,test_tool,source_ip,number_of_packets,interval_period,port=None):
        Thread.__init__(self)
        self.network_instance = network_instance
        self.destination = destination
        self.number_of_tests = int(number_of_tests)
        self.test_tool = test_tool
        self.source_ip = source_ip
        self.port = port
        self.number_of_packets = number_of_packets
        self.interval_period = int(interval_period)
        self.stop = False
        self.event_obj = Event()

    def run(self):

        target_destination = f"{self.destination}-{self.network_instance}"
        netinst = f"srbase-{self.network_instance}"
        thread_logger_name = f"target-{self.destination}"
        thread_logger_filename = f"/etc/opt/srlinux/appmgr/user_agents/history_{self.destination}.log"
        thread_logging = setup_logger(thread_logger_name,thread_logger_filename)

        if self.test_tool == 'ping':
            cmd = f"ip netns exec {netinst} ping -c {self.number_of_packets} -I {self.source_ip} {self.destination}"
            find_statement = r'rtt min/avg/max/mdev = ([\d./]+)'
        elif self.test_tool == 'httping':
            cmd = f"ip netns exec {netinst} httping -c {self.number_of_packets} -y {self.source_ip} -v {self.destination}:{self.port}"
            find_statement = r'round-trip min/avg/max/sd = ([\d./]+)'

        counting = 0

        while not self.stop and counting < self.number_of_tests:
            
            counting += 1
            successful_tests, unsuccessful_tests = get_number_of_tests(target_destination, logger)
            output = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE)
            now_ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

            if output.returncode == 0:
                service_status = True
                successful_tests = int(successful_tests)+1
                result = re.findall(find_statement,output.stdout.decode('utf-8'))
                rtt_min, rtt_avg, rtt_max, rtt_stddev = result[0].split("/")
                logger.info(f"Service available on {self.destination} !")
            else:
                service_status = False
                unsuccessful_tests = int(unsuccessful_tests)+1
                rtt_min, rtt_avg, rtt_max, rtt_stddev = 0, 0, 0, 0
                logger.info(f"Service unavailable on {self.destination} !")

            thread_logging.info(f"Source-IP: {self.source_ip} | Available: {str(service_status)} | Round-trip min/avg/max/stddev = {rtt_min}/{rtt_avg}/{rtt_max}/{rtt_stddev} ms")

            data = {
                    'last_update': { "value" : now_ts },
                    'tests_performed': int(successful_tests)+int(unsuccessful_tests),
                    'successful_tests': successful_tests,
                    'unsuccessful_tests': unsuccessful_tests,
                    'status_up': service_status,
                    'rtt_min_ms': rtt_min,
                    'rtt_avg_ms': rtt_avg,
                    'rtt_max_ms': rtt_max,
                    'rtt_stddev': rtt_stddev
            }
            Add_Telemetry( [(f'.overlay_test.state{{.IP=="{target_destination}"}}', data )] )
            self.event_obj.wait(timeout=self.interval_period)

        disable_admin_state(self.destination, logger)

################################################################
## Function that stops the tests (threads)                    ##
## It can be performed for all IP FQDN if admin_state=disable ##
## Or just for a single IP FQDN depending on the flag option  ##
################################################################
def Stop_Target_Threads(state,option,ip_fqdn=None):
    if option == "all":
        for ip_fqdn in state.targets:
            if state.targets[ ip_fqdn ]['admin_state'] == "enable":
                if state.targets[ ip_fqdn ]['thread'].is_alive():
                    state.targets[ ip_fqdn ]['thread'].stop = True
                    state.targets[ ip_fqdn ]['thread'].event_obj.set()
                    logger.info(f"Joining thread for {ip_fqdn}")
                    state.targets[ ip_fqdn ]['thread'].join()
                    threads.remove(state.targets[ ip_fqdn ]['thread'])
                del state.targets[ ip_fqdn ]['thread']
        return

    elif option == "single":
        try:
            if state.targets[ ip_fqdn ]['thread'].is_alive():
                state.targets[ ip_fqdn ]['thread'].stop = True
                state.targets[ ip_fqdn ]['thread'].event_obj.set()
                logger.info(f"Joining thread for {ip_fqdn}")
                state.targets[ ip_fqdn ]['thread'].join()
                threads.remove(state.targets[ ip_fqdn ]['thread'])
            del state.targets[ ip_fqdn ]['thread']
        except TypeError:
            logger.info(f"Error on Joining Thread :: Stop_Target_Threads()")
        else:
            return

###############################################################
## Function that starts the tests (threads)                  ##
## It can be performed for all admin_state=enable IP/FQDN    ##
## Or just for a single IP FQDN depending on the flag option ##
###############################################################
def Start_Target_Threads(state,option,ip_fqdn=None):
    if option == "all":
        for ip_fqdn in state.targets:              
            if state.targets[ ip_fqdn ]['admin_state'] == "enable":
                new_thread = ServiceMonitoringThread(
                    state.targets[ ip_fqdn ]['network_instance'],
                    ip_fqdn,
                    state.targets[ ip_fqdn ]['number_of_tests'],
                    state.targets[ ip_fqdn ]['test_tool'],
                    state.targets[ ip_fqdn ]['source_ip'],
                    state.targets[ ip_fqdn ]['number_of_packets'],
                    state.targets[ ip_fqdn ]['interval_period'],
                    state.targets[ ip_fqdn ]['port']
                )
                threads.append(new_thread)
                state.targets[ ip_fqdn ]['thread'] = new_thread
                logger.info(f" *** Starting a new Thread *** {ip_fqdn}")
                new_thread.start()
        return
    
    if option == "single":
        try:
            new_thread = ServiceMonitoringThread(
                state.targets[ ip_fqdn ]['network_instance'],
                ip_fqdn,
                state.targets[ ip_fqdn ]['number_of_tests'],
                state.targets[ ip_fqdn ]['test_tool'],
                state.targets[ ip_fqdn ]['source_ip'],
                state.targets[ ip_fqdn ]['number_of_packets'],
                state.targets[ ip_fqdn ]['interval_period'],
                state.targets[ ip_fqdn ]['port']
            )
            threads.append(new_thread)
            state.targets[ ip_fqdn ]['thread'] = new_thread
            logger.info(f" *** Starting a new Thread *** {ip_fqdn}")
            new_thread.start()
        except TypeError:
            logger.info("Error creating Thread :: Start_Target_Threads()")
        else:
            return

#####################################################################
## Function that creates or updates the target IP FQDN information ##
#####################################################################
def Update_Target(state,data,ip_fqdn):

    admin_state = data['admin_state'][12:]
    network_instance = data['network_instance']['value']
    test_tool = data['test_tool'][10:]
    number_of_tests = data['number_of_tests']['value']
    source_ip = data['source_ip']['value']
    number_of_packets = data['number_of_packets']['value']
    interval_period = data['interval_period']['value']

    if test_tool == "httping":
        port = data['port']['value']
    else:
        port = None

    if ip_fqdn not in state.targets:
        state.targets[ ip_fqdn ] = { 
            'admin_state': admin_state,
            'network_instance': network_instance,
            'test_tool': test_tool,
            'number_of_tests': number_of_tests,
            'source_ip': source_ip,
            'number_of_packets': number_of_packets,
            'interval_period': interval_period,
            'port': port,
            }
        return Existence.NEW

    else:
        state.targets[ ip_fqdn ].update( { 
            'admin_state': admin_state,
            'network_instance': network_instance,
            'test_tool': test_tool,
            'number_of_tests': number_of_tests,
            'source_ip':source_ip,
            'number_of_packets': number_of_packets,
            'interval_period': interval_period,
            'port': port,
            } )
        return Existence.ALREADY_EXISTED

############################################################################
## Proc to process the config notifications received by auto_config_agent ##
## At present processing config from js_path containing agent_name        ##
############################################################################
def Handle_Notification(obj, state):

    if obj.HasField('config'):
        logger.info(f"GOT CONFIG :: {obj.config.key.js_path}")

        json_str = obj.config.data.json.replace("'", "\"")
        data = json.loads(json_str) if json_str != "" else {}

        if obj.config.key.keys:
            ip_fqdn = obj.config.key.keys[0]

        if obj.config.op == 2 and obj.config.key.keys:
            if 'thread' in state.targets[ ip_fqdn ]:
                Stop_Target_Threads(state,"single",ip_fqdn)
            ip_fqdn_delete = ip_fqdn + '-' + state.targets[ ip_fqdn ]['network_instance']
            del state.targets[ ip_fqdn ]
            Remove_Telemetry( [(f'.overlay_test.state{{.IP=="{ip_fqdn_delete}"}}')] )

        elif 'admin_state' in data:
            state.admin_state = data['admin_state'][12:]

            if state.admin_state == "enable":
                Start_Target_Threads(state,"all")
            else:
                Stop_Target_Threads(state,"all")
  
        elif 'targets' in data:
            result = Update_Target(state, data['targets'], ip_fqdn)
            if state.targets[ ip_fqdn ]['admin_state'] == "enable" and state.admin_state == "enable":
                if result == Existence.NEW:
                    Start_Target_Threads(state,"single",ip_fqdn)
                if result == Existence.ALREADY_EXISTED:
                    if 'thread' in state.targets[ ip_fqdn ]:
                        Stop_Target_Threads(state,"single",ip_fqdn)
                    else:
                        Start_Target_Threads(state,"single",ip_fqdn)

            if state.targets[ ip_fqdn ]['admin_state'] == "disable" and state.admin_state == "enable":
                if 'thread' in state.targets[ ip_fqdn ]:
                    Stop_Target_Threads(state,"single",ip_fqdn)
 
    else:
        logger.info(f"Unexpected notification : {obj}")

    return False

##################################################################################################
## This is the main proc where all processing for ping_test starts.
## Agent registeration, notification registration, Subscrition to notifications.
## Waits on the sunscribed Notifications and once any config is received, handles that config
## If there are critical errors, Unregisters the ping_test gracefully.
##################################################################################################
def Run():

    sub_stub = sdk_service_pb2_grpc.SdkNotificationServiceStub(channel)

    response = stub.AgentRegister(request=sdk_service_pb2.AgentRegistrationRequest(agent_liveliness=3), metadata=metadata)
    logger.info(f"Registration response: {response.status}")

    th = threading.Thread(target=send_keep_alive)
    th.start()

    request = sdk_service_pb2.NotificationRegisterRequest(op=sdk_service_pb2.NotificationRegisterRequest.Create)
    create_subscription_response = stub.NotificationRegister(request=request, metadata=metadata)
    stream_id = create_subscription_response.stream_id
    logger.info(f"Create subscription response received. stream_id: {stream_id}")

    Subscribe_Notifications(stream_id)

    stream_request = sdk_service_pb2.NotificationStreamRequest(stream_id=stream_id)
    stream_response = sub_stub.NotificationStream(stream_request, metadata=metadata)

    state = State()
    count = 1

    for r in stream_response:
        logger.info(f"Count :: {count} NOTIFICATION:: \n{r.notification}")
        count += 1

        for obj in r.notification:

            if obj.HasField('config') and obj.config.key.js_path == '.commit.end':
                logger.info("TO DO -.commit.end")
            else:
                Handle_Notification(obj, state)
                logger.info(f'Updated state: {state}')
   
    return True


############################################################
## Gracefully handles SIGTERM signal                      ##
## When called, will unregister agent and gracefully exit ##
############################################################
def Exit_Gracefully(signum, frame):
    logger.info( f"Caught signal :: {signum}\n will unregister Ping Test" )

    thread_exit = True

    for thread in threads:
        if thread.is_alive():
            thread.stop = True
            thread.event_obj.set()
            thread.join()
    threads.clear()
        
    try:
        response = sdk_service_pb2.NotificationRegisterRequest(op=sdk_service_pb2.NotificationRegisterRequest.Delete)
        logger.info( f'Delete notifications :: {response}')
        response = stub.AgentUnRegister(request=sdk_service_pb2.AgentRegistrationRequest(), metadata=metadata)
        logger.info( f'Exit_Gracefully: Unregister response:: {response}' )
        #channel.close()
        sys.exit()
    except grpc._channel._Rendezvous as err:
        sys.exit()

######################################
## Main Logger Setup                ##
######################################
def setup_logger(name,log_filename,level=logging.INFO):
    logger = logging.getLogger(name)

    if logger.hasHandlers():
        return logger
    else:
        formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
        handler = RotatingFileHandler(log_filename, maxBytes=1000000, backupCount=2)
        handler.setFormatter(formatter)
        logger.setLevel(level)
        logger.addHandler(handler)
        return logger


######################################
## Main from where the Agent starts ##
## Log file is written              ##
## Signals handled                  ##
######################################
if __name__ == '__main__':

    hostname = socket.gethostname()
    stdout_dir = '/var/log/srlinux/stdout' # PyTEnv.SRL_STDOUT_DIR
    signal.signal(signal.SIGTERM, Exit_Gracefully)
    if not os.path.exists(stdout_dir):
        os.makedirs(stdout_dir, exist_ok=True)
    log_filename = '{}/{}_overlay_test.log'.format(stdout_dir,hostname)
    logger = setup_logger('main_log',log_filename)
    logger.info("START TIME :: {}".format(datetime.now()))
    if Run():
        logger.info('Agent unregistered')
    else:
        logger.info(f'Some exception caught, Check!')