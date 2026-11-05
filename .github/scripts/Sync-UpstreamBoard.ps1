#Requires -Version 7.4
<#
.SYNOPSIS
    Put upstream-board issues in the project column that matches their status label.
.DESCRIPTION
    The board is a GitHub project (user project, Status field). Its columns mirror the status
    labels on issues in the repository:
        needs-review     -> "Needs review"
        needs-decision   -> "Needs decision"
        waiting-upstream -> "Waiting upstream"
        closed issue     -> "Done"
    An open issue with exactly one status label is added to the board (if missing) and moved to
    that column. An open issue that is on the board but has no status label is removed from the
    board. An issue with more than one status label is left alone with a warning. A closed issue
    that is on the board goes to Done; a closed issue that never was on the board is ignored.

    -Issue N syncs one issue (the workflow passes the issue of the event). Without -Issue it
    syncs every open issue with a status label plus every issue already on the board.

    Only issue numbers, labels and states are read. Issue titles and bodies are never used, so
    issue text cannot reach a command. Needs GH_TOKEN with the project and repo scopes (the
    built-in workflow token cannot write to a user's project). GITHUB_GRAPHQL_URL can point at a
    test server.
.EXAMPLE
    .github/scripts/Sync-UpstreamBoard.ps1 -DryRun
.EXAMPLE
    .github/scripts/Sync-UpstreamBoard.ps1 -Issue 1048
#>
[CmdletBinding()]
param(
    [string] $Repository = 'AurimasNav/k8s-at-home',
    [string] $ProjectOwner = 'AurimasNav',
    [int] $ProjectNumber = 1,
    [int] $Issue,
    # Print what would change without changing the board.
    [switch] $DryRun
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$StatusColumns = [ordered]@{
    'needs-review'     = 'Needs review'
    'needs-decision'   = 'Needs decision'
    'waiting-upstream' = 'Waiting upstream'
}
$DoneColumn = 'Done'

function Get-TargetColumn {
    <#
    .SYNOPSIS
        Decide where an issue belongs: a column name, 'remove', or $null for "leave alone".
    #>
    param(
        [Parameter(Mandatory)] [string] $State,
        [string[]] $Labels = @(),
        [bool] $OnBoard,
        [System.Collections.Specialized.OrderedDictionary] $Columns,
        [string] $Done
    )
    if ($State -eq 'CLOSED') {
        if ($OnBoard) { return $Done }
        return $null
    }
    $status = @($Labels | Where-Object { $Columns.Contains($_) })
    if ($status.Count -gt 1) {
        Write-Host "::warning::more than one status label ($($status -join ', ')); leaving the card where it is"
        return $null
    }
    if ($status.Count -eq 1) { return $Columns[$status[0]] }
    if ($OnBoard) { return 'remove' }
    return $null
}

function Invoke-GraphQL([string] $Query, [hashtable] $Variables = @{}, [switch] $AllowNotFound) {
    $uri = if ($env:GITHUB_GRAPHQL_URL) { $env:GITHUB_GRAPHQL_URL } else { 'https://api.github.com/graphql' }
    $params = @{
        Method             = 'Post'
        Uri                = $uri
        Headers            = @{ Authorization = "Bearer $env:GH_TOKEN"; 'User-Agent' = 'upstream-board' }
        ContentType        = 'application/json'
        Body               = (@{ query = $Query; variables = $Variables } | ConvertTo-Json -Depth 10 -Compress)
        MaximumRetryCount  = 3
        RetryIntervalSec   = 5
    }
    $response = Invoke-RestMethod @params
    if ($response.PSObject.Properties['errors'] -and $response.errors) {
        $errors = @($response.errors)
        $notFound = @($errors | Where-Object { $_.PSObject.Properties['type'] -and $_.type -eq 'NOT_FOUND' })
        if (-not ($AllowNotFound -and $notFound.Count -eq $errors.Count)) {
            throw "GraphQL error: $(($errors | ForEach-Object message) -join '; ')"
        }
    }
    $response.data
}

function Get-Board {
    $data = Invoke-GraphQL -Variables @{ login = $ProjectOwner; number = $ProjectNumber } -Query @'
query($login: String!, $number: Int!) {
  user(login: $login) { projectV2(number: $number) {
    id
    field(name: "Status") { ... on ProjectV2SingleSelectField { id options { id name } } }
  } }
}
'@
    $project = $data.user.projectV2
    if (-not $project) { throw "project $ProjectOwner/$ProjectNumber not found (does the token have the project scope?)" }
    $options = @{}
    foreach ($o in $project.field.options) { $options[$o.name] = $o.id }
    foreach ($name in @($StatusColumns.Values) + $DoneColumn) {
        if (-not $options.ContainsKey($name)) { throw "the board's Status field has no '$name' option" }
    }
    [pscustomobject]@{ Id = $project.id; FieldId = $project.field.id; Options = $options }
}

function Get-IssueState([int] $Number, [string] $ProjectId) {
    $owner, $name = $Repository -split '/', 2
    $data = Invoke-GraphQL -AllowNotFound -Variables @{ owner = $owner; name = $name; number = $Number } -Query @'
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { issue(number: $number) {
    id number state
    labels(first: 50) { nodes { name } }
    projectItems(first: 50, includeArchived: true) { nodes {
      id
      project { id }
      fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
    } }
  } }
}
'@
    $i = $data.repository.issue
    if (-not $i) { return $null }  # a pull request number, or a deleted issue
    $item = @($i.projectItems.nodes | Where-Object { $_.project.id -eq $ProjectId }) | Select-Object -First 1
    [pscustomobject]@{
        Id     = $i.id
        Number = $i.number
        State  = $i.state
        Labels = @($i.labels.nodes | ForEach-Object name)
        ItemId = if ($item) { $item.id } else { $null }
        Column = if ($item -and $item.fieldValueByName) { $item.fieldValueByName.name } else { $null }
    }
}

function Get-AllIssueNumber([string] $ProjectId) {
    $owner, $name = $Repository -split '/', 2
    $numbers = [System.Collections.Generic.SortedSet[int]]::new()
    $cursor = $null
    do {
        $data = Invoke-GraphQL -Variables @{ owner = $owner; name = $name; labels = @($StatusColumns.Keys); after = $cursor } -Query @'
query($owner: String!, $name: String!, $labels: [String!], $after: String) {
  repository(owner: $owner, name: $name) {
    issues(first: 100, after: $after, states: OPEN, labels: $labels) {
      nodes { number } pageInfo { hasNextPage endCursor }
    }
  }
}
'@
        $page = $data.repository.issues
        foreach ($n in $page.nodes) { [void] $numbers.Add($n.number) }
        $cursor = $page.pageInfo.endCursor
    } while ($page.pageInfo.hasNextPage)
    $cursor = $null
    do {
        $data = Invoke-GraphQL -Variables @{ id = $ProjectId; after = $cursor } -Query @'
query($id: ID!, $after: String) {
  node(id: $id) { ... on ProjectV2 {
    items(first: 100, after: $after) {
      nodes { content { ... on Issue { number repository { nameWithOwner } } } }
      pageInfo { hasNextPage endCursor }
    }
  } }
}
'@
        $page = $data.node.items
        foreach ($n in $page.nodes) {
            if ($n.content -and $n.content.PSObject.Properties['number'] -and $n.content.repository.nameWithOwner -eq $Repository) {
                [void] $numbers.Add($n.content.number)
            }
        }
        $cursor = $page.pageInfo.endCursor
    } while ($page.pageInfo.hasNextPage)
    @($numbers)
}

function Sync-Issue([int] $Number, $Board) {
    $state = Get-IssueState -Number $Number -ProjectId $Board.Id
    if (-not $state) { Write-Host "#${Number}: not an issue, skipped"; return }
    $target = Get-TargetColumn -State $state.State -Labels $state.Labels -OnBoard ([bool] $state.ItemId) -Columns $StatusColumns -Done $DoneColumn
    $now = if ($state.ItemId) { "in '$($state.Column)'" } else { 'not on the board' }
    if (-not $target) { Write-Host "#${Number} ($($state.State.ToLower())): $now, nothing to do"; return }
    if ($target -eq $state.Column) { Write-Host "#${Number}: already in '$target'"; return }
    $verb = if ($DryRun) { 'would' } else { 'will' }
    if ($target -eq 'remove') {
        Write-Host "#${Number}: $now, no status label: $verb remove the card"
        if (-not $DryRun) {
            $null = Invoke-GraphQL -Variables @{ p = $Board.Id; i = $state.ItemId } -Query @'
mutation($p: ID!, $i: ID!) { deleteProjectV2Item(input: {projectId: $p, itemId: $i}) { deletedItemId } }
'@
        }
        return
    }
    Write-Host "#${Number}: $now, $verb move to '$target'"
    if ($DryRun) { return }
    $itemId = $state.ItemId
    if (-not $itemId) {
        $data = Invoke-GraphQL -Variables @{ p = $Board.Id; c = $state.Id } -Query @'
mutation($p: ID!, $c: ID!) { addProjectV2ItemById(input: {projectId: $p, contentId: $c}) { item { id } } }
'@
        $itemId = $data.addProjectV2ItemById.item.id
    }
    $null = Invoke-GraphQL -Variables @{ p = $Board.Id; i = $itemId; f = $Board.FieldId; o = $Board.Options[$target] } -Query @'
mutation($p: ID!, $i: ID!, $f: ID!, $o: String!) {
  updateProjectV2ItemFieldValue(input: {projectId: $p, itemId: $i, fieldId: $f, value: {singleSelectOptionId: $o}}) { projectV2Item { id } }
}
'@
}

if (-not $env:GH_TOKEN) { throw 'GH_TOKEN is not set' }
$board = Get-Board
$numbers = if ($PSBoundParameters.ContainsKey('Issue')) { @($Issue) } else { Get-AllIssueNumber -ProjectId $board.Id }
foreach ($n in $numbers) { Sync-Issue -Number $n -Board $board }
