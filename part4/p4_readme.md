# Dependencies to install

# Update & base tools
sudo apt-get update
sudo apt-get install -y build-essential git curl wget ca-certificates \
                        iproute2 iputils-ping net-tools tcpdump telnet


# Install FRRouting (FRR)
sudo apt-get install -y frr frr-pythontools

# enable IPv4 routing
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-ipforward.conf
sudo sysctl --system

# Running the OSPF Mininet script
sudo mn -c
sudo python3 p4_runner.py --input-file=p4_config.json

# To check the OSPF routes on the routers, you can use the following commands:
# In Mininet CLI, run:
## For neighbor info
r1 vtysh -c "show ip ospf neighbor"
## For routing table
r1 vtysh -c "show ip route"

# p4_runner.py contains the flapping experimentation code which helps to demonstrate the convergence time of OSPF. 
# A similar kind of experiment can be done for L3 SPF as well.

# p4_topo.py contains the topology code for Mininet. 

# p4_ospf.py contains the OSPF configuration code.