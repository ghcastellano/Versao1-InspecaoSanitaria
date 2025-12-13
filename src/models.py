from typing import List, Optional
from pydantic import BaseModel, Field

class AcaoCorretiva(BaseModel):
    descricao: str = Field(description="Descrição da ação corretiva a ser tomada.")
    prioridade: str = Field(description="Prioridade da ação: Alta, Média ou Baixa.", pattern="^(Alta|Média|Baixa)$")
    prazo_sugerido: str = Field(description="Prazo sugerido para a adequação.")

class NaoConformidade(BaseModel):
    item: str = Field(description="Item ou área onde a não conformidade foi encontrada.")
    descricao: str = Field(description="Descrição detalhada do problema.")
    legislacao_relacionada: str = Field(description="Artigo ou referência da RDC 216 violada.")
    acoes_corretivas: List[AcaoCorretiva] = Field(description="Lista de ações corretivas sugeridas.")

class RelatorioInspecao(BaseModel):
    titulo: str = Field(description="Título do relatório ou plano de ação.")
    estabelecimento: str = Field(description="Nome do estabelecimento inspecionado.", default="Não identificado")
    data_inspecao: str = Field(description="Data da inspeção ou data atual.", default_factory=lambda: datetime.now().strftime("%d/%m/%Y"))
    resumo_geral: str = Field(description="Um resumo executivo sobre a situação geral do estabelecimento.")
    nao_conformidades: List[NaoConformidade] = Field(description="Lista de não conformidades encontradas.")
    pontos_fortes: Optional[List[str]] = Field(description="Lista de pontos positivos observados (opcional).", default=[])
