FROM python:3-slim

WORKDIR /usr/src/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Values from files define in docker-compose.yml env_file section are not available here (not during build)
# but only in running container so we can't use them as parameter values for modbus2mqtt.py
# Call wrapperscript instead which calls modbus2mqtt.py with parameters from env_files
CMD [ "sh","/usr/src/app/launchModbus2mqtt"]


