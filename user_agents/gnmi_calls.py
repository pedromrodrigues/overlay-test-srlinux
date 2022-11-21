from pygnmi.client import gNMIclient

gnmi_host = ('unix:///opt/srlinux/var/run/sr_gnmi_server', 57400)

####################################################################
## Disable the admin-state of a target once the tests are completed
## 
####################################################################
def disable_admin_state(ip_fqdn,logger):

    with gNMIclient(target=gnmi_host, username='admin',password='admin',insecure=True,debug=True) as c:

        path = f"/overlay-test/targets[IP-FQDN={ip_fqdn}]/admin-state"
        try:
            update_line = [
                (
                    path,
                    "disable"
                )
            ]
            data = c.set(update=update_line,encoding='json_ietf')
            logger.info(f"gNMI OP: set ::: {data}")

        except Exception as err:
            logger.info(f"> Error in Disable_Admin_State ::: {err}")
        else:
            logger.info(f"Test Disabled Successfully!")
            return

############################################################
## Returns the number of successful and unsuccessful tests
## that are already present on the agent's state
## If no information is present on the state, it returns 0
## for both
############################################################
def get_number_of_tests(destination,logger):

    with gNMIclient(target=gnmi_host, username='admin',password='admin',insecure=True,debug=True) as c:
        try:
            data = c.get(path=[f'/overlay-test/state[IP={destination}]/successful-tests',
                f'/overlay-test/state[IP={destination}]/unsuccessful-tests'], encoding='json_ietf')
            logger.info(f"gNMI OP: get ::: {data}")

            if 'update' not in data['notification'][0]:
                return 0,0

        except KeyError as err:
            logger.info(f"> Error in Get_Number_Of_Tests ::: {err}")
            return 0,0
        else:
            return data['notification'][0]['update'][0]['val'],data['notification'][1]['update'][0]['val']