# Music Lossless

[English](README.md)

Ferramenta local para a biblioteca do app Música no Mac.

- **Trocar por Lossless / Atmos**: faixas *Enviadas*, *Correspondidas* ou *Indisponíveis* (que nunca tocam em Lossless/Atmos) são trocadas pela versão do catálogo Apple Music. Álbuns do Apple Music sem Atmos são trocados por outra edição com Dolby Atmos, quando existe.
- **Sem equivalente**: álbuns que a análise não achou no catálogo aparecem numa aba própria, com as faixas. Nada vem marcado; os que você escolher passam pelo backup e depois são apagados da biblioteca e do iCloud.
- **Repetidas**: mesma música, mesmo artista, mesmo álbum, duração até 3 s de diferença. Fica a de melhor qualidade (arquivo sem perda no Mac › Apple Music › Comprado › Correspondido › Enviado; depois maior taxa de bits).

Em toda troca:
1. As antigas que são arquivos seus (enviadas/correspondidas) são **baixadas e copiadas** para `~/Music/Backup versões antigas/Artista/Álbum/`. Se o download falhar, a faixa não é apagada. Dá para desligar isso na caixa de seleção do cartão de backup: aí os arquivos seus saem da biblioteca e do iCloud sem cópia, e as confirmações avisam.
2. Reproduções, última reprodução, nota, favorito e "não gostei" passam para a faixa que fica.
3. Nas playlists comuns, a nova entra no fim e a antiga sai. Refazer a playlist para manter a posição exata fazia o iCloud mesclar versões e duplicar a playlist inteira; por isso a alteração é a menor possível. Antes disso é salva uma cópia da ordem em `dados/playlists_antes/`. As playlists marcadas em **Playlists que o script não mexe** nunca são editadas: a antiga sai junto com a biblioteca, e a nova vai para uma lista na página para você adicionar à mão. Use para uma playlist que o iCloud insiste em duplicar.
4. A antiga é apagada da biblioteca (e do iCloud).
5. Tudo fica registrado em `dados/registro.jsonl`.

## Como usar

A interface está em **português** e **inglês**: segue o idioma do navegador e o seletor **PT / EN** no canto superior direito troca na hora.


1. Dê dois cliques em `Abrir.command` ou rode `python3 musica.py`. A página abre em http://localhost:8765.
2. **Chave MusicKit** (uma vez só): em developer.apple.com › Certificates, IDs & Profiles, crie antes um **Media ID** (Identifiers › + › Media IDs, com MusicKit marcado; o identificador começa com `media.`). Depois, em Keys › **+**, marque **Media Services (MusicKit)**, clique em Configure, escolha esse Media ID e baixe o `.p8`. Preencha o Team ID, o Key ID e o caminho do arquivo.
3. **Entrar no Apple Music** e autorizar.
4. **Analisar biblioteca**. Leva alguns minutos na primeira vez; as buscas ficam em cache.
5. Revise a lista. Vêm marcados só os álbuns de confiança **alta**: todas as faixas casaram e o álbum e o artista conferem. Use os filtros e o link "abrir" para checar os outros.
6. **Aplicar**.
7. Na aba **Sem equivalente**, se quiser, marque álbuns sem versão no catálogo para apagar. Dá para buscar, filtrar por estado e abrir cada álbum para ver as faixas e as reproduções. Os arquivos seus vão para o backup antes; se o backup de alguma faixa falhar, ela fica. Como não existe versão no catálogo, depois disso o backup é a única cópia, e as reproduções ficam só anotadas em `dados/registro.jsonl`.
8. Quando não precisar mais do backup, **Mover backup para o Lixo** manda a pasta para o Lixo do Mac. Antes, mostra quantos arquivos e quanto espaço ocupam. Fica bloqueado enquanto houver tarefa rodando ou músicas aguardando o iCloud, porque até elas chegarem o backup é a única cópia. Nada é apagado de vez até você esvaziar o Lixo.

Na primeira vez, o macOS pede permissão para o Terminal (ou o app que rodar o script) controlar o app Música.

## Observações

- Faixas sem par no catálogo ficam como estão. A lista aparece no fim da análise.
- Faixa Enviada ou Correspondida fica ligada à música do catálogo, e a Apple não adiciona a versão do catálogo enquanto ela existir. Nesses casos o script faz o backup, anota em `dados/pendentes.json` tudo o que precisa (reproduções, nota, favorito, playlists), apaga a antiga e pede a nova. O iCloud pode levar vários minutos para trazê-la ao Mac. Quando ela chega, o script conclui a troca, entre os lotes, no fim e a cada 2 min com a página aberta. Com mais de 150 pendentes, ele espera o iCloud antes do próximo lote.
- As faixas são identificadas pelo *persistent ID*. O *database ID* do app Música pode ser renumerado de uma noite para outra e nunca é usado. Antes de mexer numa faixa, o script confere se ela ainda é a mesma da análise.
- O script usa o menu Arquivo › Biblioteca › Atualizar Biblioteca na Nuvem do app Música, o que exige permissão de Acessibilidade para o app que roda o script.
- O token do Apple Music fica em `dados/token_usuario.json` (permissão 600). O `.p8` não é copiado.

## Licença

[MIT](LICENSE)
