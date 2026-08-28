for ($i = 1; $i -le 20; $i++) {
    $date = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path "contribution_log2.txt" -Value "Step commit $i at $date"
    git add contribution_log2.txt
    git commit -m "Step commit $i"
    git push origin master
    Start-Sleep -Seconds 2
}
