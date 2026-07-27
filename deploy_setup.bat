@echo off
chcp 932 > nul
echo ===================================================
echo  free-stock-site GitHub アップロード用自動スクリプト
echo ===================================================
echo.
set /p REPO_URL="GitHubで作成したリポジトリのURLを入力してEnterを押してください (例: https://github.com/username/free-stock-site.git): "

if "%REPO_URL%"=="" (
    echo [エラー] URLが入力されていません。終了します。
    pause
    exit /b
)

echo.
echo [1/4] Git 初期化中...
git init
git add .
git commit -m "Initial commit for free stock site"
git branch -M main
git remote remove origin >nul 2>&1
git remote add origin %REPO_URL%

echo.
echo [2/4] GitHub へアップロード中...
git push -u origin main

echo.
echo ===================================================
echo  アップロードが完了しました！
echo あとは GitHub の Settings - Pages で公開設定をするだけです。
echo ===================================================
pause
