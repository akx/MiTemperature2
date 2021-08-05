#!/usr/local/bin/python3.7m

import sys
import time

import requests

veraip = "192.168.1.39"
port = 3480
temperaturedeviceid = 769
humiditydeviceid = 772


# Quick and dirty script to send data to vera sensors
# sensorname=$2 temperature=$3,humidity=$4,calibratedHumidity=$5,batterylevel=$6 $7


def do_data_request(values):
	# send temperature value
	res = requests.get(
		url=f"https://{veraip}:{port}/data_request",
		params=values,
	)
	# TODO: error checking?


def main():
	temperature = sys.argv[3]  # change to sys.argv[5] for calibrated
	humidity = sys.argv[4]
	batterylevel = sys.argv[6]
	timestamp = int(time.time())

	# send temperature value
	do_data_request({
		"id": "variableset",
		"DeviceNum": temperaturedeviceid,
		"serviceId": "urn:upnp-org:serviceId:TemperatureSensor1",
		"Variable": "CurrentTemperature",
		"Value": temperature,
	})

	# send humidity value
	do_data_request({
		"id": "variableset",
		"DeviceNum": humiditydeviceid,
		"serviceId": "urn:micasaverde-com:serviceId:HumiditySensor1",
		"Variable": "CurrentLevel",
		"Value": humidity,
	})

	# send battery and timestamp to temp and humidity virtual sensors
	for device in (humiditydeviceid, temperaturedeviceid):
		do_data_request({
			"id": "variableset",
			"DeviceNum": device,
			"serviceId": "urn:micasaverde-com:serviceId:HaDevice1",
			"Variable": "LastUpdate",
			"Value": timestamp,
		})
		do_data_request({
			"id": "variableset",
			"DeviceNum": device,
			"serviceId": "urn:micasaverde-com:serviceId:HaDevice1",
			"Variable": "BatteryLevel",
			"Value": batterylevel,
		})


if __name__ == '__main__':
	main()
