#!/bin/bash

set -ex

if [[ -v VIRTUAL_ENV ]]
then
  PIP_OPT=""
else
  PIP_OPT="--user"
fi

python_version=$(python -V)
echo "Will run Python lint using ${python_version}"

uv pip install ruff ${PIP_OPT}
ruff --version

uv pip uninstall opencue_proto opencue_pycue opencue_pyoutline opencue_cueadmin opencue_cueman opencue_cuecmd opencue_cuesubmit opencue_rqd
if [[ -v OPENCUE_PROTO_PACKAGE_PATH ]]
then
  echo "Installing pre-built cuebot package"
  uv pip install ${OPENCUE_PROTO_PACKAGE_PATH} ${PIP_OPT}
else
  uv pip install ./proto ${PIP_OPT}
fi

echo "Running lint for pycue/..."
uv pip install ./pycue[test] ${PIP_OPT}
ruff check pycue/FileSequence pycue/opencue pycue/tests

echo "Running lint for pyoutline/..."
uv pip install ./pyoutline[test] ${PIP_OPT}
ruff check pyoutline/outline pyoutline/tests

echo "Running lint for cueadmin/..."
uv pip install ./cueadmin[test] ${PIP_OPT}
ruff check cueadmin/cueadmin cueadmin/tests

echo "Running lint for cueman/..."
uv pip install ./cueman[test] ${PIP_OPT}
ruff check cueman/cueman cueman/tests

echo "Running lint for cuecmd/..."
uv pip install ./cuecmd[test] ${PIP_OPT}
ruff check cuecmd/cuecmd cuecmd/tests

echo "Running lint for cuegui/..."
uv pip install ./cuegui[test] ${PIP_OPT}
ruff check cuegui/cuegui cuegui/tests --exclude cuegui/cuegui/images --exclude cuegui/cuegui/images/crystal

echo "Running lint for cuesubmit/..."
uv pip install ./cuesubmit[test] ${PIP_OPT}
ruff check cuesubmit/cuesubmit cuesubmit/tests

echo "Running lint for rqd/..."
uv pip install ./rqd[test] ${PIP_OPT}
ruff check rqd/rqd rqd/tests rqd/pytests
