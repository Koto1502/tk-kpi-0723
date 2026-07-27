@echo off
setlocal
cd /d "%~dp0"
uv run --with google-cloud-bigquery --with db-dtypes --with pandas --with pyarrow python generate_churn.py
if errorlevel 1 exit /b %errorlevel%
echo.
echo Refreshed churn.html
