#!/bin/bash
set -e
serverIp=159.89.154.87
owner=PyTorchLightning
repo=pytorch-lightning-Pair1-after
eval "$(ssh-agent -s)"
chmod 600 root_key
ssh-keyscan $serverIp >> ~/.ssh/known_hosts
ssh-add root_key
pvm=$(python3 -V | cut -c8)
if [ $pvm -eq 2 ]
then
    echo "mypy not supported on this version"
    exit 1
fi
pvs=$(python3 -V | cut -c10)
if [ $pvs -eq 5 ]
then
    yes | scp -i root_key "root@$serverIp:~/mypy-0.800-cp35-cp35m-linux_x86_64.whl" ./mypy-0.800-cp35-cp35m-linux_x86_64.whl
    pip install ./mypy-0.800-cp35-cp35m-linux_x86_64.whl
elif [ $pvs -eq 6 ]
then
    yes | scp -i root_key "root@$serverIp:~/mypy-0.800-cp36-cp36m-linux_x86_64.whl" ./mypy-0.800-cp36-cp36m-linux_x86_64.whl
    pip install ./mypy-0.800-cp36-cp36m-linux_x86_64.whl
elif [ $pvs -eq 7 ]
then
    yes | scp -i root_key "root@$serverIp:~/mypy-0.800-cp37-cp37m-linux_x86_64.whl" ./mypy-0.800-cp37-cp37m-linux_x86_64.whl
    pip install ./mypy-0.800-cp37-cp37m-linux_x86_64.whl
elif [ $pvs -eq 8 ]
then
    yes | scp -i root_key "root@$serverIp:~/mypy-0.800-cp38-cp38-linux_x86_64.whl" ./mypy-0.800-cp38-cp38-linux_x86_64.whl
    pip install ./mypy-0.800-cp38-cp38-linux_x86_64.whl
elif [ $pvs -eq 9 ]
then
    yes | scp -i root_key "root@$serverIp:~/mypy-0.800-cp39-cp39-linux_x86_64.whl" ./mypy-0.800-cp39-cp39-linux_x86_64.whl
    pip install ./mypy-0.800-cp39-cp39-linux_x86_64.whl
else
    echo "mypy not supported on this version"
    exit 1
fi
pip install astunparse
pip install typed-ast
pip install typed-astunparse
python3 typecheck.py
zip -r mypy_test_cache.zip mypy_test_cache/
pv=$(python3 -V | cut -c8-10)
arc=$(uname -m)
yes | scp -i root_key ./mypy_test_cache.zip "root@$serverIp:~/cache/$owner---$repo\($pv---$arc\).zip"
yes | scp -i root_key ./mypy_test_report.txt "root@$serverIp:~/report/$owner---$repo\($pv---$arc\).txt"
yes | scp -i root_key ./reveal_locals_location.csv "root@$serverIp:~/reveal_locals/$owner---$repo\($pv---$arc\).csv"
