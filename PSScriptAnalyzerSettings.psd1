@{
    # Repo-wide PowerShell lint settings (CI: .github/workflows/iac.yaml, job "static").
    Severity     = @('Error', 'Warning')
    ExcludeRules = @(
        # Scripts are CLIs run in CI; host output is the intended interface.
        'PSAvoidUsingWriteHost'
    )
}
