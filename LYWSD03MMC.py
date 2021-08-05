#!/usr/bin/python3 -u
#!/home/openhabian/Python3/Python-3.7.4/python -u
# -u to unbuffer output. Otherwise when calling with nohup or redirecting output things are printed very lately or would even mixup
import dataclasses

from bluepy import btle
import argparse
import os
import sys
import re
from dataclasses import dataclass
from collections import deque
import socket
import threading
import time
import signal
import traceback
import math
import logging
import json
import requests
import configparser
import bluetooth_utils


print("---------------------------------------------")
print("MiTemperature2 / ATC Thermometer version 3.1")
print("---------------------------------------------")


@dataclass(frozen=True)
class Measurement:
	temperature: float
	humidity: int
	voltage: float
	calibratedHumidity: int = 0
	battery: int = 0
	timestamp: int = 0
	sensorname: str = ""
	rssi: int = 0

	def __eq__(self, other):  # rssi may be different, so exclude it from comparison
		if (
			self.temperature == other.temperature
			and self.humidity == other.humidity
			and self.calibratedHumidity == other.calibratedHumidity
			and self.battery == other.battery
			and self.sensorname == other.sensorname
		):
			# in atc mode also exclude voltage as it changes often due to frequent measurements
			return True if args.atc else (self.voltage == other.voltage)
		else:
			return False

	def print(self):
		print(f"Temperature: {self.temperature}")
		print(f"Humidity: {self.humidity}")
		if self.voltage:
			print(f"Battery voltage: {self.voltage} V")
		if self.battery:
			print(f"Battery: {self.battery}%")
		if self.rssi:
			print(f"RSSI: {self.rssi} dBm")
		if self.calibratedHumidity != self.humidity:
			print(f"Humidity calibrated: {self.calibratedHumidity}")



measurements = deque()
# globalBatteryLevel=0
previousMeasurements = {}
identicalCounters = {}
MQTTClient = None
MQTTTopic = None
receiver = None
subtopics = []
mqttJSONDisabled = False
sock = None  # from ATC
lastBLEPaketReceived = 0
BLERestartCounter = 1
args = None  # populated in main()
adress = None  # populated in main()
connected = False  # run_device_mode()
unconnectedTime = None  # run_device_mode() and watchdog


def myMQTTPublish(topic, jsonMessage):
	if subtopics:
		messageDict = json.loads(jsonMessage)
		for subtopic in subtopics:
			print("Topic:", subtopic)
			MQTTClient.publish(f"{topic}/{subtopic}", messageDict[subtopic], 0)
	if not mqttJSONDisabled:
		MQTTClient.publish(topic, jsonMessage, 1)


def signal_handler(sig, frame):
	if args.atc:
		bluetooth_utils.disable_le_scan(sock)
	sys.exit(0)


def watchDog_Thread():
	global unconnectedTime
	global connected
	while True:
		logging.debug("watchdog_Thread")
		logging.debug(f"unconnectedTime : {unconnectedTime}")
		logging.debug(f"connected : {connected}")
		logging.debug(f"pid : {os.getpid()}")
		now = int(time.time())
		if (unconnectedTime is not None) and ((now - unconnectedTime) > 60):  # could also check connected is False, but this is more fault proof
			kill_bluepy_helper()
			unconnectedTime = now  # reset unconnectedTime to prevent multiple killings in a row
		time.sleep(5)


def thread_SendingData():
	global previousMeasurements
	global measurements
	path = os.path.dirname(os.path.abspath(__file__))

	while True:
		try:
			ret = None
			mea = measurements.popleft()
			if mea.sensorname in previousMeasurements:
				prev = previousMeasurements[mea.sensorname]
				# only send data when it has changed or X identical data has been skipped,
				# ~10 packets per minute, 50 packets --> writing at least every 5 minutes
				if mea == prev and identicalCounters[mea.sensorname] < args.skipidentical:
					print(f"Measurements for {mea.sensorname} are identical; don't send data\n")
					identicalCounters[mea.sensorname] += 1
					continue

			if args.callback:
				fmt = "sensorname,temperature,humidity,voltage"  # don't try to seperate by semicolon ';' os.system will use that as command seperator
				if " " in mea.sensorname:
					sensorname = f'"{mea.sensorname}"'
				else:
					sensorname = mea.sensorname
				params = f"{sensorname} {mea.temperature} {mea.humidity} {mea.voltage}"
				if args.TwoPointCalibration or args.offset:  # would be more efficient to generate fmt only once
					fmt += ",humidityCalibrated"
					params += f" {mea.calibratedHumidity}"
				if args.battery:
					fmt += ",batteryLevel"
					params += f" {mea.battery}"
				if args.rssi:
					fmt += ",rssi"
					params += f" {mea.rssi}"
				params += f" {mea.timestamp}"
				fmt += ",timestamp"
				cmd = f"{path}/{args.callback} {fmt} {params}"
				print(cmd)
				ret = os.system(cmd)

			if args.httpcallback:
				url = args.httpcallback.format(
					sensorname=mea.sensorname,
					temperature=mea.temperature,
					humidity=mea.humidity,
					voltage=mea.voltage,
					humidityCalibrated=mea.calibratedHumidity,
					batteryLevel=mea.battery,
					rssi=mea.rssi,
					timestamp=mea.timestamp,
				)
				print(url)
				ret = 0
				try:
					r = requests.get(url, verify=False, timeout=1)
					r.raise_for_status()
				except requests.exceptions.RequestException as e:
					ret = str(e)

			if ret:
				measurements.appendleft(mea)  # put the measurement back
				print(f"Data couldn't be send to Callback ({ret}), retrying...")
				time.sleep(5)  # wait before trying again
			else:  # data was sent
				# replace() without changes is a shallow copy
				previousMeasurements[mea.sensorname] = dataclasses.replace(mea)
				identicalCounters[mea.sensorname] = 0

		except IndexError:
			# print("No Data")
			time.sleep(1)
		except Exception as e:
			print(e)
			print(traceback.format_exc())


def keepingLEScanRunning():  # LE-Scanning gets disabled sometimes, especially if you have a lot of BLE connections, this thread periodically enables BLE scanning again
	global BLERestartCounter
	while True:
		time.sleep(1)
		now = time.time()
		if now - lastBLEPaketReceived > args.watchdogtimer:
			print(f"Watchdog: Did not receive any BLE Paket within {int(now - lastBLEPaketReceived)}s. Restarting BLE scan. Count: {BLERestartCounter}")
			bluetooth_utils.disable_le_scan(sock)
			bluetooth_utils.enable_le_scan(sock, filter_duplicates=False)
			BLERestartCounter += 1
			print("")
			time.sleep(5)  # give some time to take effect


def calibrateHumidity2Points(humidity, offset1, offset2, calpoint1, calpoint2):
	# offset1=args.offset1
	# offset2=args.offset2
	# p1y=args.calpoint1
	# p2y=args.calpoint2
	p1y = calpoint1
	p2y = calpoint2
	p1x = p1y - offset1
	p2x = p2y - offset2
	m = (p1y - p2y) * 1.0 / (p1x - p2x)  # y=mx+b
	# b = (p1x * p2y - p2x * p1y) * 1.0 / (p1y - p2y)
	b = p2y - m * p2x  # would be more efficient to do this calculations only once
	humidityCalibrated = m * humidity + b
	if humidityCalibrated > 100:  # with correct calibration this should not happen
		humidityCalibrated = 100
	elif humidityCalibrated < 0:
		humidityCalibrated = 0
	humidityCalibrated = int(round(humidityCalibrated, 0))
	return humidityCalibrated


class MyDelegate(btle.DefaultDelegate):
	def __init__(self, params):
		btle.DefaultDelegate.__init__(self)
		# ... initialise here

	def handleNotification(self, cHandle, data):
		global measurements
		try:
			if args.influxdb == 1:
				timestamp = int((time.time() // 10) * 10)
			else:
				timestamp = int(time.time())
			temp = int.from_bytes(data[0:2], byteorder="little", signed=True) / 100
			# print("Temp received: " + str(temp))
			if args.round:
				# print("Temperatur unrounded: " + str(temp

				if args.debounce:
					temp *= 10
					intpart = math.floor(temp)
					fracpart = round(temp - intpart, 1)
					# print("Fracpart: " + str(fracpart))
					if fracpart >= 0.7:
						mode = "ceil"
					elif fracpart <= 0.2:  # either 0.8 and 0.3 or 0.7 and 0.2 for best even distribution
						mode = "trunc"
					else:
						mode = None
					# print("Modus: " + mode)
					if mode == "trunc":  # only a few times
						temp = math.trunc(temp)
					elif mode == "ceil":
						temp = math.ceil(temp)
					else:
						temp = round(temp, 0)
					temp /= 10.0
					# print("Debounced temp: " + str(temp))
				else:
					temp = round(temp, 1)
			humidity = int.from_bytes(data[2:3], byteorder="little")
			voltage = int.from_bytes(data[3:5], byteorder="little") / 1000.0
			batteryLevel = min(int(round((voltage - 2.1), 2) * 100), 100)  # 3.1 or above --> 100% 2.1 --> 0 %
			humidityCalibrated = humidity

			if args.offset:
				humidityCalibrated = humidity + args.offset

			if args.TwoPointCalibration:
				humidityCalibrated = calibrateHumidity2Points(humidity, args.offset1, args.offset2, args.calpoint1, args.calpoint2)

			measurement = Measurement(
				temperature=temp,
				humidity=humidity,
				voltage=voltage,
				calibratedHumidity=humidityCalibrated,
				battery=batteryLevel,
				timestamp=timestamp,
				sensorname=args.name,
				rssi=0,
			)

			measurement.print()

			if args.callback or args.httpcallback:
				measurements.append(measurement)

			if args.mqttconfigfile:
				jsonString = buildJSONString(measurement)
				myMQTTPublish(MQTTTopic, jsonString)
				# MQTTClient.publish(MQTTTopic,jsonString,1)

		except Exception as e:
			print("Fehler")
			print(e)
			print(traceback.format_exc())


def kill_bluepy_helper():
	pid = os.getpid()
	# It seems that sometimes bluepy-helper remains and thus prevents a reconnection, so we try killing our own bluepy-helper
	# we want to kill only bluepy from our own process tree, because other python scripts have there own bluepy-helper process
	pstree = os.popen(f"pstree -p {pid}").read()
	try:
		bluepypid = re.findall(r"bluepy-helper\((.*)\)", pstree)[0]  # Store the bluepypid, to kill it later
		os.system(f"kill {bluepypid}")
		logging.debug(f"Killed bluepy with pid: {bluepypid}")
	except IndexError:  # Should normally occur because we're disconnected
		logging.debug("Couldn't find pid of bluepy-helper")


def connect():
	# print("Interface: " + str(args.interface))
	p = btle.Peripheral(adress, iface=args.interface)
	val = b"\x01\x00"
	p.writeCharacteristic(0x0038, val, True)  # enable notifications of Temperature, Humidity and Battery voltage
	p.writeCharacteristic(0x0046, b"\xf4\x01\x00", True)
	p.withDelegate(MyDelegate("abc"))
	return p


def buildJSONString(measurement):
	return json.dumps(
		{
			"temperature": str(measurement.temperature),
			"humidity": str(measurement.humidity),
			"voltage": str(measurement.voltage),
			"calibratedHumidity": str(measurement.calibratedHumidity),
			"battery": str(measurement.battery),
			"timestamp": str(measurement.timestamp),
			"sensor": str(measurement.sensorname),
			"rssi": str(measurement.rssi),
			"receiver": receiver,
		}
	)


def MQTTOnConnect(client, userdata, flags, rc):
	print(f"MQTT connected with result code {rc}")


def MQTTOnPublish(client, userdata, mid):
	print(f"MQTT published, Client: {client} Userdata: {userdata} mid: {mid}")


def MQTTOnDisconnect(client, userdata, rc):
	print(f"MQTT disconnected, Client: {client} Userdata: {userdata} RC: {rc}")


def make_argument_parser():
	parser = argparse.ArgumentParser(allow_abbrev=False)
	parser.add_argument("--device", "-d", help="Set the device MAC-Address in format AA:BB:CC:DD:EE:FF", metavar="AA:BB:CC:DD:EE:FF")
	parser.add_argument("--battery", "-b", help="Get estimated battery level, in ATC-Mode: Get battery level from device", metavar="", type=int, nargs="?", const=1)
	parser.add_argument("--count", "-c", help="Read/Receive N measurements and then exit script", metavar="N", type=int)
	parser.add_argument("--interface", "-i", help="Specifiy the interface number to use, e.g. 1 for hci1", metavar="N", type=int, default=0)
	parser.add_argument("--unreachable-count", "-urc", help="Exit after N unsuccessful connection tries", metavar="N", type=int, default=0)
	parser.add_argument("--mqttconfigfile", "-mcf", help="specify a configurationfile for MQTT-Broker")

	rounding = parser.add_argument_group("Rounding and debouncing")
	rounding.add_argument("--round", "-r", help="Round temperature to one decimal place", action="store_true")
	rounding.add_argument("--debounce", "-deb", help="Enable this option to get more stable temperature values, requires -r option", action="store_true")

	offsetgroup = parser.add_argument_group("Offset calibration mode")
	offsetgroup.add_argument("--offset", "-o", help="Enter an offset to the reported humidity value", type=int)

	complexCalibrationGroup = parser.add_argument_group("2 Point Calibration")
	complexCalibrationGroup.add_argument("--TwoPointCalibration", "-2p", help="Use complex calibration mode. All arguments below are required", action="store_true")
	complexCalibrationGroup.add_argument("--calpoint1", "-p1", help="Enter the first calibration point", type=int)
	complexCalibrationGroup.add_argument("--offset1", "-o1", help="Enter the offset for the first calibration point", type=int)
	complexCalibrationGroup.add_argument("--calpoint2", "-p2", help="Enter the second calibration point", type=int)
	complexCalibrationGroup.add_argument("--offset2", "-o2", help="Enter the offset for the second calibration point", type=int)

	callbackgroup = parser.add_argument_group("Callback related arguments")
	callbackgroup.add_argument("--callback", "-call", help="Pass the path to a program/script that will be called on each new measurement")
	callbackgroup.add_argument("--httpcallback", "-http", help="Pass the URL to a program/script that will be called on each new measurement")
	callbackgroup.add_argument("--name", "-n", help="Give this sensor a name reported to the callback script")
	callbackgroup.add_argument("--skipidentical", "-skip", help="N consecutive identical measurements won't be reported to callbackfunction", metavar="N", type=int, default=0)
	callbackgroup.add_argument("--influxdb", "-infl", help="Optimize for writing data to influxdb,1 timestamp optimization, 2 integer optimization", metavar="N", type=int, default=0)

	atcgroup = parser.add_argument_group("ATC mode related arguments")
	atcgroup.add_argument("--atc", "-a", help="Read the data of devices with custom ATC firmware flashed, use --battery to get battery level additionaly in percent", action="store_true")
	atcgroup.add_argument("--watchdogtimer", "-wdt", metavar="X", type=int, help="Re-enable scanning after not receiving any BLE packet after X seconds")
	atcgroup.add_argument("--devicelistfile", "-df", help="Specify a device list file giving further details to devices")
	atcgroup.add_argument("--onlydevicelist", "-odl", help="Only read devices which are in the device list file", action="store_true")
	atcgroup.add_argument("--rssi", "-rs", help="Report RSSI via callback", action="store_true")
	return parser


def setup_mqtt(args):
	global mqttJSONDisabled
	global MQTTClient
	global MQTTTopic
	global receiver
	try:
		import paho.mqtt.client as mqtt
	except ImportError:
		print("Please install MQTT-Library via 'pip/pip3 install paho-mqtt'")
		exit(1)
	if not os.path.exists(args.mqttconfigfile):
		print(f"Error MQTT config file '{args.mqttconfigfile}' not found")
		sys.exit(1)
	mqttConfig = configparser.ConfigParser()
	# print(mqttConfig.sections())
	mqttConfig.read(args.mqttconfigfile)
	broker = mqttConfig["MQTT"]["broker"]
	port = int(mqttConfig["MQTT"]["port"])
	username = mqttConfig["MQTT"]["username"]
	password = mqttConfig["MQTT"]["password"]
	MQTTTopic = mqttConfig["MQTT"]["topic"]
	lastwill = mqttConfig["MQTT"]["lastwill"]
	lwt = mqttConfig["MQTT"]["lwt"]
	clientid = mqttConfig["MQTT"]["clientid"]
	receiver = mqttConfig["MQTT"]["receivername"]
	subtopics = mqttConfig["MQTT"]["subtopics"]
	if len(subtopics) > 0:
		subtopics = subtopics.split(",")
		if "nojson" in subtopics:
			subtopics.remove("nojson")
			mqttJSONDisabled = True
	if not receiver:
		receiver = socket.gethostname()
	client = mqtt.Client(clientid)
	client.on_connect = MQTTOnConnect
	client.on_publish = MQTTOnPublish
	client.on_disconnect = MQTTOnDisconnect
	client.reconnect_delay_set(min_delay=1, max_delay=60)
	client.loop_start()
	client.username_pw_set(username, password)
	if len(lwt) > 0:
		print(f"Using lastwill with topic: {lwt} and message: {lastwill}")
		client.will_set(lwt, lastwill, qos=1)
	client.connect_async(broker, port)
	MQTTClient = client


def main():
	parser = make_argument_parser()
	args = parser.parse_args()

	if args.mqttconfigfile:
		setup_mqtt(args)

	if args.TwoPointCalibration:
		if not (args.calpoint1 and args.offset1 and args.calpoint2 and args.offset2):
			print("In 2 Point calibration you have to enter 4 points")
			sys.exit(1)
		elif args.offset:
			print("Offset calibration and 2 Point calibration can't be used together")
			sys.exit(1)

	if not args.name:
		args.name = args.device

	if args.callback or args.httpcallback:
		dataThread = threading.Thread(target=thread_SendingData)
		dataThread.start()

	signal.signal(signal.SIGINT, signal_handler)

	if args.device:
		run_device_mode(args)
	elif args.atc:
		run_atc_mode(args)
	else:
		parser.print_help()
		sys.exit(1)


def run_device_mode(args):
	global unconnectedTime
	global connected
	global adress

	if re.match("[0-9a-fA-F]{2}([:]?)[0-9a-fA-F]{2}(\\1[0-9a-fA-F]{2}){4}$", args.device):
		adress = args.device
	else:
		print("Please specify device MAC-Address in format AA:BB:CC:DD:EE:FF")
		sys.exit(1)
	p = btle.Peripheral()
	cnt = 0
	connected = False
	# logging.basicConfig(level=logging.DEBUG)
	logging.basicConfig(level=logging.ERROR)
	logging.debug("Debug: Starting script...")
	unconnectedTime = None
	connectionLostCounter = 0
	watchdogThread = threading.Thread(target=watchDog_Thread)
	watchdogThread.start()
	logging.debug("watchdogThread started")
	while True:
		try:
			if not connected:
				print(f"Trying to connect to {adress}")
				p = connect()
				connected = True
				unconnectedTime = None

			if p.waitForNotifications(2000):
				# handleNotification() was called

				cnt += 1
				if args.count is not None and cnt >= args.count:
					print(f"{args.count} measurements collected. Exiting in a moment.")
					p.disconnect()
					time.sleep(5)
					kill_bluepy_helper()
					sys.exit(0)
				print("")
				continue
		except Exception as e:
			print("Connection lost")
			connectionLostCounter += 1
			if connected is True:  # First connection abort after connected
				unconnectedTime = int(time.time())
				connected = False
			if args.unreachable_count != 0 and connectionLostCounter >= args.unreachable_count:
				print("Maximum numbers of unsuccessful connections reaches, exiting")
				sys.exit(0)
			time.sleep(1)
			logging.debug(e)
			logging.debug(traceback.format_exc())

		print("Waiting...")
	# Perhaps do something else here


def run_atc_mode(args):
	print("Script started in ATC Mode")
	print("----------------------------")
	print("In this mode all devices within reach are read out, unless a devicelistfile and --onlydevicelist is specified.")
	print("Also --name Argument is ignored, if you require names, please use --devicelistfile.")
	print("In this mode rounding and debouncing are not available, since ATC firmware sends out only one decimal place.")
	print('ATC mode usually requires root rights. If you want to use it with normal user rights, \nplease execute "sudo setcap cap_net_raw,cap_net_admin+eip $(eval readlink -f `which python3`)"')
	print("You have to redo this step if you upgrade your python version.")
	print("----------------------------")
	import bluetooth._bluetooth as bluez

	advCounter = dict()
	sensors = dict()
	if args.devicelistfile:
		# import configparser
		if not os.path.exists(args.devicelistfile):
			print(f"Error specified device list file '{args.devicelistfile}' not found")
			sys.exit(1)
		sensors = configparser.ConfigParser()
		sensors.read(args.devicelistfile)
		# Convert macs in devicelist file to Uppercase
		sensorsnew = {}
		for key in sensors:
			sensorsnew[key.upper()] = sensors[key]
		sensors = sensorsnew
	if args.onlydevicelist and not args.devicelistfile:
		print("Error: --onlydevicelist requires --devicelistfile <devicelistfile>")
		sys.exit(1)
	dev_id = args.interface  # the bluetooth device is hci0
	bluetooth_utils.toggle_device(dev_id, True)
	try:
		sock = bluez.hci_open_dev(dev_id)
	except Exception:
		print("Cannot open bluetooth device %i" % dev_id)
		raise
	bluetooth_utils.enable_le_scan(sock, filter_duplicates=False)

	def le_advertise_packet_handler(mac, adv_type, data, rssi):
		global lastBLEPaketReceived
		lastBLEPaketReceived = time.time()
		data_str = bluetooth_utils.raw_packet_to_str(data)
		preeamble = "10161a18"
		paketStart = data_str.find(preeamble)
		offset = paketStart + len(preeamble)
		# print("reveived BLE packet")+
		atcData_str = data_str[offset : offset + 26]
		ATCPaketMAC = atcData_str[0:12].upper()
		macStr = mac.replace(":", "").upper()
		atcIdentifier = data_str[(offset - 4) : offset].upper()

		if ((atcIdentifier != "1A18" or ATCPaketMAC != macStr) or args.onlydevicelist) and (
			(atcIdentifier != "1A18" or mac not in sensors) or len(atcData_str) != 26
		):  # skip data not from ATC devices, double checked
			return

		advNumber = atcData_str[-2:]
		lastAdvNumber = advCounter.get(macStr)
		if lastAdvNumber == advNumber:  # Nothing to do here
			return
		advCounter[macStr] = advNumber
		print(f"BLE packet: {mac} {adv_type:02x} {data_str} {rssi:d}")
		# print("AdvNumber: ", advNumber)
		# temp = data_str[22:26].encode('utf-8')
		# temperature = int.from_bytes(bytearray.fromhex(data_str[22:26]),byteorder='big') / 10.
		global measurements
		if args.influxdb == 1:
			timestamp = int((time.time() // 10) * 10)
		else:
			timestamp = int(time.time())

		# temperature = int(data_str[22:26],16) / 10.
		temperature = int.from_bytes(bytearray.fromhex(atcData_str[12:16]), byteorder="big", signed=True) / 10.0
		humidity = int(atcData_str[16:18], 16)
		batteryVoltage = int(atcData_str[20:24], 16) / 1000
		batteryPercent = int(atcData_str[18:20], 16)

		currentMQTTTopic = MQTTTopic
		sensorname = mac
		humidityCalibrated = humidity  # may be calibrated later
		if mac in sensors:
			sensor_info = sensors[mac]
			try:
				sensorname = sensor_info["sensorname"]
			except KeyError:
				pass

			if "offset1" in sensor_info and "offset2" in sensor_info and "calpoint1" in sensor_info and "calpoint2" in sensor_info:
				humidityCalibrated = calibrateHumidity2Points(humidity, int(sensor_info["offset1"]), int(sensor_info["offset2"]), int(sensor_info["calpoint1"]), int(sensor_info["calpoint2"]))
			elif "humidityOffset" in sensor_info:
				humidityCalibrated = humidity + int(sensor_info["humidityOffset"])

			if "topic" in sensor_info:
				currentMQTTTopic = sensor_info["topic"]

		measurement = Measurement(
			temperature=temperature,
			humidity=humidity,
			voltage=batteryVoltage,
			calibratedHumidity=humidityCalibrated,
			battery=batteryPercent,
			timestamp=timestamp,
			sensorname=sensorname,
			rssi=rssi,
		)

		measurement.print()

		if args.callback or args.httpcallback:
			measurements.append(measurement)

		if args.mqttconfigfile:
			jsonString = buildJSONString(measurement)
			myMQTTPublish(currentMQTTTopic, jsonString)
		# MQTTClient.publish(currentMQTTTopic,jsonString,1)

		# print("Length:", len(measurements))
		print("")

	try:
		if args.watchdogtimer:
			keepingLEScanRunningThread = threading.Thread(target=keepingLEScanRunning)
			keepingLEScanRunningThread.start()
			logging.debug("keepingLEScanRunningThread started")

		# Blocking call (the given handler will be called each time a new LE
		# advertisement packet is detected)
		bluetooth_utils.parse_le_advertising_events(sock, handler=le_advertise_packet_handler, debug=False)
	except KeyboardInterrupt:
		bluetooth_utils.disable_le_scan(sock)


if __name__ == "__main__":
	main()
