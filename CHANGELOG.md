# Changelog

All notable changes to this project will be documented in this file.

## [1.0.83] - 2025-10-15
- added highlight on index page for which plant in the list is feeding
- modified draining logic to include the level sensor on the remote system in additiona to train flow. 
- fixed draining so it looks at the remote Sensor 3 (empty) to draining and not just flow

## [1.0.82] - 2025-10-11
- added Nutrient Calculator

## [1.0.81] - 2025-09-21
- added 2 hours timeout for "Feeding in Progress"

## [1.0.80] - 2025-09-21
- added API command to toggle valve by name

## [1.0.79] - 2025-09-20
- added additional info container on main page

## [1.0.78] - 2025-09-20
- added "feeding in progress" to prevent ph from adjusting during feeding. Also prevents notifications from being sent.
- modified index page to have expaint HTML container for "feeding in progress" which disables notifications and auto valve triggering"
- added ability to save pump calibration dates for dosing pumps

## [1.0.77] - 2025-08-15
- fixed install script for permissions

## [1.0.76] - 2025-08-14

### Added
- Logic to prevent pH dosing if none of the water sensors detect water.
- Plant name color change based on pH range: green for good, red for high or low.
- Modified log logic to support multiple log files for different things.
- 6-hour logging interval to track pH.
- system will not dispense ph up/down if the bucket is empty
- added color coding to index page to visually indicate if pH is within range
