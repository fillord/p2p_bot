import sys
from passlib.context import CryptContext

# Спрашиваем пароль и выводим его хеш
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
if len(sys.argv) < 2:
    print("Использование: python3 hash_password.py 'ваш_пароль'")
else:
    password = sys.argv[1]
    hashed_password = pwd_context.hash(password)
    print("Ваш хеш пароля (скопируйте всю строку):")
    print(hashed_password)