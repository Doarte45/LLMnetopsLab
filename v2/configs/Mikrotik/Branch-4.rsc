/system identity set name=Branch-4

/ip address add address=172.16.20.6/30 interface=ether1
/ip address add address=10.2.1.1/24 interface=ether2
/ip address add address=10.2.2.1/24 interface=ether3

/routing ospf instance add name=default-v2 version=2 router-id=10.255.2.1
/routing ospf area add name=area20 area-id=0.0.0.20 instance=default-v2 type=stub
/routing ospf interface-template add area=area20 networks=172.16.20.4/30
/routing ospf interface-template add area=area20 networks=10.2.1.0/24,10.2.2.0/24 passive

/ip firewall filter add chain=forward connection-state=established,related action=accept comment="allow return traffic"
/ip firewall filter add chain=forward src-address=10.2.2.0/24 dst-address=10.0.0.0/8 action=drop comment="guest LAN: no access to internal networks"
/ip firewall filter add chain=forward src-address=10.0.0.0/8 dst-address=10.2.2.0/24 action=drop comment="guest LAN: no new connections from internal networks"
