"""cs-cheat-radar: analise de partidas para triagem de suspeitas de cheat.

Nada aqui toca no processo de jogo nenhum. As fontes sao arquivos locais
(demos, replays convertidos, logs de servidor), recursos que os proprios jogos
publicam de proposito (Game State Integration, RCON, logaddress) e APIs
publicas de plataforma.

Nao existe, e nao vai existir, modulo que contorne anticheat: para ler posicao
e mira dos outros jogadores durante a partida seria preciso ler a memoria do
cliente, e um programa que faz isso e um cheat - qualquer que seja a intencao
de quem escreveu.
"""

__version__ = "4.0.0"
