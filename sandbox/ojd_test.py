#!/usr/bin/python3
from openjd.model import model_to_object, JobParameterDefinition, decode_job_template
from openjd.model.v2023_09 import *
import json
import yaml

command = CommandString('echo test 12434')
action = Action(command=command)

layer1 = Step(
    name="layer1",
    script=StepScript(
        actions=StepActions(
            onRun=action
        )
    ),
    parameterSpace=StepParameterSpace(
        taskParameterDefinitions={
            "tags": RangeListTaskParameterDefinition(
                type=TaskParameterType.STRING,
                range=["blender"]
            ),
            "services": RangeListTaskParameterDefinition(
                type=TaskParameterType.STRING,
                range=["blender"]
            ),
            "frames": RangeListTaskParameterDefinition(
                type=TaskParameterType.INT,
                range=["1-1"]
            ),
            "outputs": RangeListTaskParameterDefinition(
                type=TaskParameterType.STRING,
                range=["/tmp/blender/blender.#####.exr"]
            )
        },
        combination="(tags, services, frames)"
    )
)

job = Job(
    name='testJob',
    steps=[layer1],
    parameters={"show": JobParameter(value="testing", type=JobParameterType.STRING),
                "shot": JobParameter(value="sh0010", type=JobParameterType.STRING),
                "user": JobParameter(value="jimmy", type=JobParameterType.STRING),
                "email": JobParameter(value="jimmy@example.com", type=JobParameterType.STRING),
                "uid": JobParameter(value="1000", type=JobParameterType.INT),
                "facility": JobParameter(value="local", type=JobParameterType.STRING),
                "paused": JobParameter(value="0", type=JobParameterType.INT),
                "priority": JobParameter(value="1", type=JobParameterType.INT),
                "maxretries": JobParameter(value="2", type=JobParameterType.INT),
                "autoeat": JobParameter(value="0", type=JobParameterType.INT)
                }
)
obj = model_to_object(model=job)
print(yaml.dump(obj))
