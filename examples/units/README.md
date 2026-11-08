# Evaluation dummies

These units only sleep. They are not actual camera/IMU/perception/control services.
They make the authored `examples/robot.yaml` and its generated Requisite/After
bundle usable without hardware. Nothing installs or enables them automatically.

After reviewing names/paths and ensuring these names are not already in use,
you may manually copy the four unit files and the generated dependency drop-ins
to your user manager configuration, then explicitly daemon-reload. Start Stages
through devdash, inspect their states, and click Next for each later Stage.

Stop control, perception, then sensors explicitly before removing demo files.
Do not copy these over existing real service definitions. See
`docs/stage-configuration.md` for ownership, obsolete drop-ins and rollback.
