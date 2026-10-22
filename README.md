# Logger for Cellular Modem Diagnostics for Blue Robotics BlueBoat (Quectel Modem)

## Setup instructions

Follow the instruction from the Blue Robotics website to set up the cellular modem: https://bluerobotics.com/learn/installing-the-blueboat-cellular-modem

Once you are successfully connected to the Blue Boat over cellular, do the following:

1. In BlueOS, enter pirate mode and open up a terminal
2. From the terminal enter ```red-pill``` to escape the docker container
3. Navigate to the userdata directory ```cd usr/blueos/userdata```
4. Clone this repo in the userdata directory
5. Navigate to this repo ```cd blueboat-cellular-testing``` and execute the logging script with ```./cell-modem-diagnostics.py```

## Understanding the data

The diagnostic data will print to the terminal and write to a CSV.
