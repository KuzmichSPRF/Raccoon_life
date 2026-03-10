Set fso = CreateObject(\"Scripting.FileSystemObject\")  
Set f = fso.CreateTextFile(\"c:\Users\kuzmi\Raccoon_life\backend\main.py\", True)  
f.WriteLine \"from fastapi import FastAPI^\"  
