/system identity set name=Branch-3

/ip address add address=172.16.10.10/30 interface=ether1
/ip address add address=10.1.3.1/24 interface=ether2
/ip address add address=172.16.20.2/30 interface=ether3

/routing ospf instance add name=default-v2 version=2 router-id=10.255.1.3
/routing ospf area add name=area10 area-id=0.0.0.10 instance=default-v2
/routing ospf interface-template add area=area10 networks=172.16.10.8/30
/routing ospf interface-template add area=area10 networks=172.16.20.0/30
/routing ospf interface-template add area=area10 networks=10.1.3.0/24 passive
