from stacker.blueprints.base import Blueprint

from troposphere import (
    Output, Ref, GetAtt, GetAZs, NoValue,
    Template, Join, Tags, Region, Select
)

from troposphere.ec2 import (
    VPC, Subnet, InternetGateway,
    VPCGatewayAttachment, RouteTable,
    Route, SubnetRouteTableAssociation,
    EIP, NatGateway, SecurityGroup
)


class EKSVPC(Blueprint):
    """
    VPC Resources
     * VPC
     * Public and Private subnets
     * Internet Gateway
     * NAT Gateway
     * Route Tables

    Private subnets for worker nodes and public subnets for Kubernetes to create internet-facing load balancers
    https://docs.aws.amazon.com/en_us/eks/latest/userguide/create-public-private-vpc.html
    """
    VARIABLES = {
        "BaseCidr": {
            "type": str,
            "description": "The two first octets of the VPC CIDR string."
        },
        "CreatePrivateSubnets": {
            "type": str,
            "description": "Set to false to create only public subnets.",
            "default": "true"
        }
    }


    def create_template(self):
        t = self.template

        t.add_description("Amazon EKS - VPC")

        variables = self.get_variables()
        vpc_base_cidr = variables["BaseCidr"]
        create_private_subnets = variables["CreatePrivateSubnets"]
        private_subnets = []
        basename = "{}Eks".format(self.context.namespace).replace("-", "")

        vpc = self.create_vpc(
            basename,
            vpc_base_cidr
        )

        public_subnet_cidrs = [
            "{}.24.0/24".format(vpc_base_cidr),
            "{}.32.0/24".format(vpc_base_cidr)
        ]

        public_subnet_route_table = self.create_route_table(
            basename,
            vpc,
            public=True
        )

        public_subnets = self.create_subnet(
            basename,
            vpc,
            public_subnet_cidrs,
            public_subnet_route_table,
            public=True
        )

        internet_gateway = self.create_internet_gateway(
            basename,
            vpc
        )

        self.add_route(
            "{}InternetGateway".format(basename),
            public_subnet_route_table,
            internet_gateway,
            NoValue,
            "0.0.0.0/0"
        )

        if create_private_subnets == "true":

            private_subnet_cidrs = [
                "{}.40.0/24".format(vpc_base_cidr),
                "{}.48.0/24".format(vpc_base_cidr)
            ]

            private_subnet_route_table = self.create_route_table(
                basename,
                vpc
            )

            private_subnets = self.create_subnet(
                basename,
                vpc,
                private_subnet_cidrs,
                private_subnet_route_table
            )

            nat_gateway, nat_eip = self.create_nat_gateway(
                basename,
                public_subnets[0]
            )

            self.add_route(
                "{}NatGateway".format(basename),
                private_subnet_route_table,
                NoValue,
                nat_gateway,
                "0.0.0.0/0"
            )

            t.add_output(
                Output(
                    "NATEIP", Value=Ref(nat_eip)
                )
            )

        # https://docs.aws.amazon.com/en_us/eks/latest/userguide/create-public-private-vpc.html#vpc-create-sg
        control_plane_sg = self.create_security_group(basename, vpc)

        t.add_output(
            Output(
                "VPCID", Value=Ref(vpc)
            )
        )

        t.add_output(
            Output(
                "PrivateSubnets", Value=Join(",", [Ref(subnet) for subnet in private_subnets])
            )
        )

        t.add_output(
            Output(
                "PublicSubnets", Value=Join(",", [Ref(subnet) for subnet in public_subnets])
            )
        )

        t.add_output(
            Output(
                "ClusterControlPlaneSecurityGroup", Value=Ref(control_plane_sg)
            )
        )


    def create_vpc(self, basename, vpc_base_cidr):
        t = self.template

        return t.add_resource(
            VPC(
                '{}Vpc'.format(basename),
                # VPC must have DNS hostname and DNS resolution support.
                # Otherwise, worker nodes cannot register with cluster
                EnableDnsSupport=True,
                EnableDnsHostnames=True,
                CidrBlock="{}.0.0/16".format(vpc_base_cidr),
                Tags=Tags(
                    Name=basename
                )
            )
        )


    def create_route_table(self, basename, vpc, public=False):
        t = self.template

        privacy = "Public" if public == True else "Private"
        internal_elb = "0" if public == True else "1"

        return t.add_resource(
            RouteTable(
                '{0}{1}RouteTable'.format(basename, privacy),
                VpcId=Ref(vpc),
                Tags=Tags({
                    "Name": "{0} {1} Subnets".format(basename, privacy),
                    "Network": privacy,
                    "kubernetes.io/role/internal-elb": internal_elb
                })
            )
        )


    def create_subnet(self, basename, vpc, cidrs, route_table, public=False):

        if type(cidrs) is not list:
            raise TypeError("fatal: cidrs must be a list of cidrs, "
                            "e.g. ['1.2.3.0/24']. received '{}'".format(cidrs))

        t = self.template

        privacy = "Public" if public == True else "Private"
        subnets = []

        for index, cidr in enumerate(cidrs):
            zone_num = str(index + 1)

            subnet = t.add_resource(
                Subnet(
                    '{0}{1}Subnet{2}'.format(basename, privacy, zone_num),
                    CidrBlock=cidr,
                    VpcId=Ref(vpc),
                    AvailabilityZone=Select(str(index), GetAZs(Region)),
                    MapPublicIpOnLaunch=public,
                    Tags=Tags({
                        "Name": "{0} {1} Subnet Zone {2}".format(basename, privacy, zone_num),
                        "Network": privacy,
                        # https://docs.aws.amazon.com/en_us/eks/latest/userguide/network_reqs.html
                        "kubernetes.io/cluster/{}".format(basename): "shared"
                    })
                )
            )

            t.add_resource(
                SubnetRouteTableAssociation(
                    '{0}{1}SubnetZone{2}RouteTableAssociation'.format(basename, privacy, zone_num),
                    SubnetId=Ref(subnet),
                    RouteTableId=Ref(route_table),
                )
            )

            subnets.append(subnet)

        return subnets


    def create_nat_gateway(self, basename, subnet):
        t = self.template

        nat_eip = t.add_resource(
            EIP(
                '{}NatEIP'.format(basename),
                Domain="vpc",
                DependsOn='{}VPCGatewayAttachment'.format(basename)
            )
        )

        nat_gateway = t.add_resource(
            NatGateway(
                '{}NatGateway'.format(basename),
                AllocationId=GetAtt(nat_eip, "AllocationId"),
                SubnetId=Ref(subnet),
                DependsOn='{}VPCGatewayAttachment'.format(basename)
            )
        )

        return nat_gateway, nat_eip


    def create_internet_gateway(self, basename, vpc):
        t = self.template

        internetGateway = t.add_resource(
            InternetGateway(
                '{}InternetGateway'.format(basename),
                Tags=Tags(
                    Name=basename,
                    Network="Public"
                )
            )
        )

        t.add_resource(
            VPCGatewayAttachment(
                '{}VPCGatewayAttachment'.format(basename),
                VpcId=Ref(vpc),
                InternetGatewayId=Ref(internetGateway)
            )
        )

        return internetGateway


    def add_route(self, route_name, route_table,
                internet_gateway, nat_gateway, destination):

        t = self.template

        if internet_gateway == NoValue:
            return t.add_resource(Route(
                "{}Route".format(route_name),
                RouteTableId=Ref(route_table),
                NatGatewayId=Ref(nat_gateway),
                DestinationCidrBlock=destination
            ))
        else:
            return t.add_resource(Route(
                "{}Route".format(route_name),
                RouteTableId=Ref(route_table),
                GatewayId=Ref(internet_gateway),
                DestinationCidrBlock=destination
            ))


    def create_security_group(self, basename, vpc):
        t = self.template

        return t.add_resource(
            SecurityGroup(
                "{}ControlPlaneSecurityGroup".format(basename),
                GroupDescription='Cluster communication with worker nodes',
                VpcId=Ref(vpc)
            ))