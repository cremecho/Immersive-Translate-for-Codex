param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$Destination
)
$ErrorActionPreference = 'Stop'
$word = $null
$document = $null
$discardChanges = 0
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $word.AutomationSecurity = 3
    $oldUpdateLinks = $word.Options.UpdateLinksAtOpen
    $word.Options.UpdateLinksAtOpen = $false
    $no = $false
    $readOnly = $true
    $password = ''
    $docxFormat = 16
    $document = $word.Documents.Open([ref]$Source, [ref]$no, [ref]$readOnly, [ref]$no, [ref]$password)
    $document.SaveAs2([ref]$Destination, [ref]$docxFormat)
}
finally {
    try {
        if ($null -ne $document) { $document.Close([ref]$discardChanges) }
    } finally {
        if ($null -ne $word) {
            if ($null -ne $oldUpdateLinks) { $word.Options.UpdateLinksAtOpen = $oldUpdateLinks }
            $word.Quit([ref]$discardChanges)
        }
    }
}
