/system identity set name=Branch-4

/ip address add address=172.16.20.6/30 interface=ether1
/ip address add address=10.2.1.1/24 interface=ether2
/ip address add address=10.2.2.1/24 interface=ether3

/routing ospf instance add name=default-v2 version=2 router-id=10.255.2.1
/routing ospf area add name=area20 area-id=0.0.0.20 instance=default-v2
/routing ospf interface-template add area=area20 networks=172.16.20.4/30
/routing ospf interface-template add area=area20 networks=10.2.1.0/24,10.2.2.0/24 passive
