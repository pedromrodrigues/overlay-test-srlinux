#!/bin/bash
###########################################################################
# Description:
#     This script will launch the python script of snmp_agent
#     (forwarding any arguments passed to this script).
#
# Copyright (c) 2018 Nokia
###########################################################################


_term (){
    echo "Caugth signal SIGTERM !! "
    kill -TERM "$child" 2>/dev/null
}

function main()
{
    trap _term SIGTERM
    local virtual_env="/etc/opt/srlinux/appmgr/venv-dev/bin/activate"
    local main_module="/etc/opt/srlinux/appmgr/user_agents/overlay_test.py"

    # source the virtual-environment, which is used to ensure the correct python packages are installed,
    # and the correct python version is used
    source "${virtual_env}"
    export  PYTHONPATH="$PYTHONPATH:/etc/opt/srlinux/appmgr/user_agents:/opt/srlinux/bin:/usr/lib/python3.6/site-packages/sdk_protos:/etc/opt/srlinux/appmgr/venv-dev/lib/python3.6/site-packages"
    export http_proxy=""
    export https_proxy=""
    python3 ${main_module} &

    child=$! 
    wait "$child"

}

main "$@"