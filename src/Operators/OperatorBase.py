from abc import ABC, abstractmethod

# Typing imports
from src.DBHandler import DBHandler
from typing import List


class Operator(ABC):
    def __init__(self, k):
        self.k = k

    def run(self, db: DBHandler, additionals: str = "") -> List[int]:
        sql = self.create_sql_query(db, additionals=additionals)
        result = db.execute_and_fetchall(sql)
        return [r[0] for r in result[:self.k]]
        
    @abstractmethod
    def cost(self) -> int:
        raise NotImplementedError
    
    @abstractmethod
    def create_sql_query(self, db: DBHandler, additionals: str = "") -> str:
        raise NotImplementedError
    