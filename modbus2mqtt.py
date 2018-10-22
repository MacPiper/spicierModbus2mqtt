# spicierModbus2mqtt - Modbus TCP/RTU to MQTT bridge (and vice versa)
# https://github.com/mbs38/spicierModbus2mqtt 
#
# Written in 2018 by Max Brueggemann <mail@maxbrueggemann.de>
#  
#
# Provided under the terms of the MIT license.

# Contains a bunch of code taken from:
# modbus2mqtt - Modbus master with MQTT publishing
# Written and (C) 2015 by Oliver Wagner <owagner@tellerulam.com>
# Provided under the terms of the MIT license.

# Main improvements over modbus2mqtt:
# - more abstraction when writing to coils/registers using mqtt. Writing is now
#   possible without having to know slave id, reference, function code etc.
# - specific coils/registers can be made read only
# - multiple slave devices on one bus are now supported
# - polling speed has been increased sgnificantly. With modbus RTU @ 38400 baud
#   more than 80 transactions per second have been achieved.
# - switched over to pymodbus which is in active development


# Requires:
# - Eclipse Paho for Python - http://www.eclipse.org/paho/clients/python/
# - pymodbus - https://github.com/riptideio/pymodbus
# 

import argparse
import logging
import logging.handlers
import time
import socket
import paho.mqtt.client as mqtt
import serial
import io
import sys
import csv
import signal

from pymodbus.pdu import ModbusRequest
from pymodbus.client.sync import ModbusSerialClient as SerialModbusClient
from pymodbus.client.sync import ModbusTcpClient as TCPModbusClient
from pymodbus.transaction import ModbusRtuFramer

version="0.1"
    
parser = argparse.ArgumentParser(description='Bridge between ModBus and MQTT')
parser.add_argument('--mqtt-host', default='localhost', help='MQTT server address. Defaults to "localhost"')
parser.add_argument('--mqtt-port', default='1883', type=int, help='MQTT server port. Defaults to 1883')
parser.add_argument('--mqtt-topic', default='modbus/', help='Topic prefix to be used for subscribing/publishing. Defaults to "modbus/"')
#parser.add_argument('--clientid', default='modbus2mqtt', help='Client ID prefix for MQTT connection')
parser.add_argument('--rtu', help='pyserial URL (or port name) for RTU serial port')
parser.add_argument('--rtu-baud', default='19200', type=int, help='Baud rate for serial port. Defaults to 19200')
parser.add_argument('--rtu-parity', default='even', choices=['even','odd','none'], help='Parity for serial port. Defaults to even')
parser.add_argument('--tcp', help='Act as a Modbus TCP master, connecting to host TCP')
parser.add_argument('--tcp-port', default='502', type=int, help='Port for Modbus TCP. Defaults to 502')
parser.add_argument('--config', required=True, help='Configuration file. Required!')
#parser.add_argument('--log', help='set log level to the specified value. Defaults to WARNING. Use DEBUG for maximum detail')
parser.add_argument('--syslog', action='store_true', help='enable logging to syslog')
parser.add_argument('--force', default='0',type=int, help='publish values after "force" seconds since publish regardless of change. Defaults to 0 (change only)')
args=parser.parse_args()

globaltopic=args.mqtt_topic
if not globaltopic.endswith("/"):
    globaltopic+="/"

logging.info('Starting spicier modbus2mqtt V%s with topic prefix \"%s\"' %(version, globaltopic))

def signal_handler(signal, frame):
        print('Exiting ' + sys.argv[0])
        sys.exit(0)
signal.signal(signal.SIGINT, signal_handler)

master=None

deviceList=[]

class Device:
    def __init__(self,name,slaveid):
        self.name=name
        self.occupiedTopics=[]
        self.writableReferences=[]
        self.slaveid=slaveid
        logging.info('Added new device \"'+self.name+'\"')


class Poller:
    def __init__(self,topic,rate,slaveid,functioncode,reference,size):
        self.topic=topic
        self.rate=float(rate)
        self.slaveid=int(slaveid)
        self.functioncode=int(functioncode)
        self.reference=int(reference)
        self.size=int(size)
        self.next_due=0
        self.last = None
        self.readableReferences=[]
        self.device=None

        for myDev in deviceList:
            if myDev.name == self.topic:
                self.device=myDev
                break
        if self.device == None:
            device = Device(self.topic,slaveid)
            deviceList.append(device)
            self.device=device
            
    def poll(self):
#        try:
        if True:
            result = None

            if self.functioncode == 3:
                result = master.read_holding_registers(self.reference, self.size, unit=self.slaveid)
                data = result.registers
            if self.functioncode == 1:
                result = master.read_coils(self.reference, self.size, unit=self.slaveid)
                data = result.bits
            if self.functioncode == 2:
                result = master.read_discrete_inputs(self.reference, self.size, unit=self.slaveid)
                data = result.bits
            if self.functioncode == 4:
                result = master.read_input_registers(self.reference, self.size, unit=self.slaveid)
                data = result.registers

            for ref in self.readableReferences:
                ref.checkPublish(data,self.topic)

    def checkPoll(self):
        if time.clock_gettime(0) >= self.next_due:
            self.poll()
            self.next_due=time.clock_gettime(0)+self.rate

    def addReference(self,myRef):
        #check reference configuration and maybe add to this poller or to the list of writable things
        if myRef.topic not in self.device.occupiedTopics:
            self.device.occupiedTopics.append(myRef.topic)
            if "r" in myRef.rw or "w" in myRef.rw:
                myRef.device=self.device
                if "r" in myRef.rw:
                    if myRef.checkSanity(self.reference,self.size):
                        self.readableReferences.append(myRef)
                    else:
                        print("Reference \""+str(myRef.reference)+"\" with topic "+myRef.topic+" is not in range ("+str(self.reference)+" to "+str(int(self.reference+self.size))+") of poller \""+self.topic+"\", therefore ignoring it for polling.")
                if "w" in myRef.rw:
                    if self.functioncode == 3: #holding registers
                        myRef.writefunctioncode=6 #preset single register
                    if self.functioncode == 1: #coils
                        myRef.writefunctioncode=5 #force single coil
                    if self.functioncode == 2: #read input status, not writable
                        print("Reference \""+str(myRef.reference)+"\" with topic "+myRef.topic+" in poller \""+self.topic+"\" is not writable (discrete input)")
                    if self.functioncode == 4: #read input register, not writable
                        print("Reference \""+str(myRef.reference)+"\" with topic "+myRef.topic+" in poller \""+self.topic+"\" is not writable (input register)")
                    if myRef.writefunctioncode is not None:
                       self.device.writableReferences.append(myRef)
            else:
                print("Reference \""+str(myRef.reference)+"\" with topic "+myRef.topic+" in poller \""+self.topic+"\" is neither read nor writable, therefore ignoring it.")
        else:
            print("Reference topic ("+str(myRef.topic)+") is already occupied, therefore ignoring it.")

class Reference:
    def __init__(self,topic,reference,format,rw):
        self.topic=topic
        self.reference=int(reference)
        self.format=format.split(":",2)
        self.lastval=None
        self.rw=rw
        self.relativeReference=None
        self.writefunctioncode=None
        self.device=None

    def checkSanity(self,reference,size):
        if self.reference in range(reference,size+reference):
            self.relativeReference=self.reference-reference
            return True

    def checkPublish(self,result,topic):
        if self.lastval != result[self.relativeReference]:
            self.lastval= result[self.relativeReference]
            #print(topic+"/state/"+self.topic)
            #print(globaltopic)
            mqc.publish(globaltopic+self.device.name+"/state/"+self.topic,self.lastval,qos=0,retain=True)
            
        
##########################################################################################
pollers=[]

# type, topic, slaveid,   ref, size, functioncode, rate
# type, topic, reference, rw,      ,             ,

# Now lets read the config file
with open(args.config,"r") as csvfile:
   # dialect=csv.Sniffer().sniff(csvfile.read(8192))
    csvfile.seek(0)
    reader=csv.DictReader(csvfile)
    currentPoller=None
    for row in reader:
        if row["type"]=="poller" or row["type"]=="poll":
            if row["col5"] == "holding_register":
                functioncode = 3
            if row["col5"] == "coil":
                functioncode = 1
            if row["col5"] == "input_register":
                functioncode = 4
            if row["col5"] == "input_status":
                functioncode = 2
            rate = float(row["col6"])
            slaveid = int(row["col2"])
            reference = int(row["col3"])
            size = int(row["col4"])
            currentPoller = Poller(row["topic"],rate,slaveid,functioncode,reference,size)
            pollers.append(currentPoller)
            continue
        elif row["type"]=="reference" or row["type"]=="ref":
            reference = int(row["col2"])
            currentPoller.addReference(Reference(row["topic"],reference,"",row["col3"]))



def messagehandler(mqc,userdata,msg):
    if True:
#    try:

        (prefix,device,function,reference) = msg.topic.split("/")
        if function != 'set':
            return
        myRef = None
        myDevice = None
        for iterDevice in deviceList:
            if iterDevice.name == device:
                myDevice = iterDevice
        if myDevice == None: # no such device
            return
        for iterRef in myDevice.writableReferences:
            if iterRef.topic == reference:
                myRef=iterRef
        if myRef == None: # no such reference
            return    

#        print("Writing to device "+str(myDevice.name)+", Slave-ID="+str(myDevice.slaveid)+" at Reference="+str(myRef.reference)+" using function code "+str(myRef.writefunctioncode))
        logging.info("Writing to device "+str(myDevice.name)+", Slave-ID="+str(myDevice.slaveid)+" at Reference="+str(myRef.reference)+" using function code "+str(myRef.writefunctioncode))

        payload = str(msg.payload.decode("utf-8"))
        if myRef.writefunctioncode == 5:
            value = None
            if payload == 'True':
                value = True
            if payload == 'False':
                value = False
            if value != None:
                result = master.write_coil(int(myRef.reference),value,unit=int(myRef.device.slaveid))
        
        if myRef.writefunctioncode == 6:
            try:
                value=int(payload)
                if value > 65535 or value < 0:
                    value = None
            except:
                value=None
            if value is not None:
                #print(value)
                result = master.write_registers(int(myRef.reference),value,unit=myRef.device.slaveid)
        
def connecthandler(mqc,userdata,flags,rc):
    logging.info("Connected to MQTT broker with rc=%d" % (rc))
    mqc.subscribe(globaltopic+"+/set/+")
    mqc.publish(globaltopic+"connected",1,qos=1,retain=True)

def disconnecthandler(mqc,userdata,rc):
    logging.warning("Disconnected from MQTT broker with rc=%d" % (rc))

#    try:
if True:
    clientid=globaltopic + "-" + str(time.time())
    mqc=mqtt.Client(client_id=clientid)
    mqc.on_connect=connecthandler
    mqc.on_message=messagehandler
    mqc.on_disconnect=disconnecthandler
    mqc.will_set(globaltopic+"connected",0,qos=2,retain=True)
    mqc.disconnected = True
    mqc.connect(args.mqtt_host,args.mqtt_port,60)
    mqc.loop_start()
    if args.rtu:
        
        if args.rtu_parity=="none":
            parity = "N"
        if args.rtu_parity=="odd":
            parity = "O"
        if args.rtu_parity=="even":
            parity = "E"

        master = SerialModbusClient(method="rtu", port=args.rtu, stopbits = 1, bytesize = 8, parity = parity, baudrate = int(args.rtu_baud), timeout=1)

    elif args.tcp:
        master = TCPModbusClient(args.tcp, args.tcp_port)
    else:
        print("You must specify a modbus access method, either --rtu or --tcp")
        sys.exit(1)
    master.connect()

while True:
    for p in pollers:
        p.checkPoll()
    time.sleep(0.001)




