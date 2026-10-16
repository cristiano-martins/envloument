/* Descarrega as credenciais de demonstração num ficheiro .txt */
function baixarCredenciais(usuario, senha, expira) {
  var val = new Date(expira).toLocaleString('pt-PT', { dateStyle: 'medium', timeStyle: 'short' });
  var linhas = ['ENVLOUMENT - ACESSO A DEMONSTRACAO', '', 'Endereco:      ' + location.origin + '/demo/login',
    'Utilizador:    ' + usuario, 'Palavra-passe: ' + senha, 'Valido ate:    ' + val, '',
    'Acesso temporario e pessoal. Guarda este ficheiro em local seguro.',
    'Se perderes a palavra-passe, usa "Esqueci a palavra-passe" na pagina de login.'];
  var blob = new Blob([linhas.join('\r\n')], { type: 'text/plain;charset=utf-8' });
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'envloument-credenciais.txt';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
}
