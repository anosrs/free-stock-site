@echo off
chcp 932 > nul
cd /d "%~dp0"
echo ===================================================
echo  1分間隔 リアルタイム自動巡回＆更新ループスクリプト
echo ===================================================
echo この画面を開いている間、1分ごとに自動で巡回してサイトを更新します。
echo.

git config user.name "anosrs" >nul 2>&1
git config user.email "anosrs@users.noreply.github.com" >nul 2>&1

:loop
echo ---------------------------------------------------
echo [%date% %time%] 入荷情報を巡回中...
py auto_builder.py

git add .
git commit -m "Auto update at %time%" >nul 2>&1
if %errorlevel% equ 0 (
    echo [%date% %time%] 新着商品を検出！ GitHubへ自動アップロード中...
    git push -u origin main >nul 2>&1
    echo アップロード完了！
) else (
    echo 新着なし (更新不要)
)

echo 次の巡回まで 60 秒待機します...
timeout /t 60 /nobreak > nul
goto loop
