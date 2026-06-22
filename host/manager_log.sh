#!/bin/bash
# Follow the manager's log. Ctrl+C stops WATCHING only -- the manager keeps running.
exec docker logs -f --tail 40 gopro_manager
