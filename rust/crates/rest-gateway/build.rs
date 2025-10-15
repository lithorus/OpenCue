fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Compile all proto files
    let proto_files = vec![
        "../../../proto/src/comment.proto",
        "../../../proto/src/criterion.proto",
        "../../../proto/src/cue.proto",
        "../../../proto/src/department.proto",
        "../../../proto/src/depend.proto",
        "../../../proto/src/facility.proto",
        "../../../proto/src/filter.proto",
        "../../../proto/src/host.proto",
        "../../../proto/src/job.proto",
        "../../../proto/src/limit.proto",
        "../../../proto/src/renderPartition.proto",
        "../../../proto/src/report.proto",
        "../../../proto/src/rqd.proto",
        "../../../proto/src/service.proto",
        "../../../proto/src/show.proto",
        "../../../proto/src/subscription.proto",
        "../../../proto/src/task.proto",
    ];

    tonic_build::configure()
        .build_server(false) // We're only a client
        .build_client(true)
        .compile(&proto_files, &["../../../proto/src/"])?;

    Ok(())
}