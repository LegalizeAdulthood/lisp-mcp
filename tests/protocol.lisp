;;; Shared fixtures exercise the real codec in separate endpoint packages.

(load (merge-pathnames "../lisp-mcp.lisp" *load-truename*))

(load (merge-pathnames "service-fixture.lisp" *load-truename*))

(defpackage #:protocol-tests (:use #:cl))
(in-package #:protocol-tests)
(defvar *checks* 0)
(defvar *mcp-v1-test-sentinel* nil)

(defun check (expected actual)
  (incf *checks*)
  (unless (equalp expected actual)
    (error "Expected ~S, got ~S" expected actual)))
(defun rejects (function)
  (check t (handler-case (progn (funcall function) nil) (error () t))))

(defun fixtures ()
  (with-open-file (stream (merge-pathnames "protocol-fixtures.json" *load-truename*))
    (coerce (yason:parse stream) 'list)))

(dolist (fixture (fixtures))
  (let ((text (gethash "wire" fixture))
        (direction (if (string= (gethash "direction" fixture) "request")
                       :request :response)))
    ;; YASON represents JSON false as NIL by default.
    (if (gethash "valid" fixture)
        (let* ((host (lisp-mcp::mcp-v1-decode text direction))
               (listener (genera-protocol-fixture::mcp-v1-decode text direction)))
          (check host listener)
          (check text (lisp-mcp::mcp-v1-encode host direction))
          (check text (genera-protocol-fixture::mcp-v1-encode listener direction)))
        (progn
          (rejects (lambda () (lisp-mcp::mcp-v1-decode text direction)))
          (rejects (lambda ()
                     (genera-protocol-fixture::mcp-v1-decode text direction)))))))
(check nil *mcp-v1-test-sentinel*)
(check nil (find-symbol "PROTOCOL-UNINTERNED" "KEYWORD"))
(check nil (find-symbol "PROTOCOL-UNINTERNED" "LISP-MCP"))
(check nil (find-symbol "PROTOCOL-UNINTERNED" "GENERA-PROTOCOL-FIXTURE"))

;; Reader/printer customization cannot change parsing or serialization.
(let ((*read-base* 16) (*read-eval* t) (*readtable* (copy-readtable))
      (*print-base* 16) (*print-radix* t) (*print-case* :downcase)
      (*print-level* 0) (*print-length* 0) (*print-pretty* t))
  (set-macro-character #\! (lambda (&rest ignored)
                            (declare (ignore ignored))
                            (setf *mcp-v1-test-sentinel* t)))
  (let ((text "(:version 1 :id \"safe\" :op :hello :client \"!\")"))
    (check text (lisp-mcp::mcp-v1-encode
                 (lisp-mcp::mcp-v1-decode text :request) :request)))
  (rejects (lambda () (lisp-mcp::mcp-v1-parse "!")))
  (check nil *mcp-v1-test-sentinel*))

(dolist (parse (list #'lisp-mcp::mcp-v1-parse
                    #'genera-protocol-fixture::mcp-v1-parse))
  (check 9223372036854775807 (funcall parse "9223372036854775807"))
  (check -9223372036854775808 (funcall parse "-9223372036854775808"))
  (check nil (funcall parse "()"))
  (check '(:version 1 :id "x" :op :hello)
         (funcall parse "(:VERSION 1 :ID \"x\" :OP :HELLO)"))
  (dolist (bad '("-9223372036854775809" "00000000000000000000" "(" ")"
                 "\"unterminated" "#(1)" "" "nil nil"))
    (rejects (lambda () (funcall parse bad))))
  (rejects (lambda () (funcall parse (make-string 1048577 :initial-element #\Space))))
  (let ((nested (concatenate 'string (make-string 16 :initial-element #\()
                             "0" (make-string 16 :initial-element #\)))))
    (check t (listp (funcall parse nested)))
    (rejects (lambda () (funcall parse (concatenate 'string "(" nested ")")))))
  (let ((full (format nil "(~{~A~^ ~})" (make-list 4095 :initial-element "nil"))))
    (check 4095 (length (funcall parse full)))
    (rejects (lambda () (funcall parse
                                (concatenate 'string "(nil " (subseq full 1)))))))

(dolist (encode (list #'lisp-mcp::mcp-v1-encode
                     #'genera-protocol-fixture::mcp-v1-encode))
  (dolist (bad (list 3 '(:version) '(:version 1 :id "x" :op :hello . t)
                    (list :version 1 :id "x" :op :hello :client (make-symbol "X"))
                    (list :version 1 :id "x" :op :hello :client #\a)
                    (list :version 1 :id (make-string 129 :initial-element #\a)
                          :op :hello)))
    (rejects (lambda () (funcall encode bad :request))))
  (let ((cycle (list :version 1 :id "cycle" :op :hello)))
    (setf (cdr (last cycle)) cycle)
    (rejects (lambda () (funcall encode cycle :request))))
  (let ((large (make-string 1048576 :initial-element #\a)))
    (rejects (lambda ()
               (funcall encode (list :version 1 :id "x" :op :hello :client large)
                        :request))))
  (let ((escaped (make-string 524288 :initial-element #\")))
    (rejects (lambda ()
               (funcall encode (list :version 1 :id "x" :op :hello :client escaped)
                        :request)))))

;; Raw envelope framing bypasses Genera diagnostic/character translation.
(let* ((text (format nil "(:version 1 :id \"raw\" :op :hello :client \"~C~C~C\")"
                     #\Tab #\Linefeed #\Return))
       (wire (format nil "~D ~A" (length text) text)))
  (check text (genera-protocol-fixture::mcp-read-request
               (make-string-input-stream wire) nil))
  (let* ((response (list :version 1 :id "raw" :ok t :stdout text))
         (encoded (lisp-mcp::mcp-v1-encode response :response)))
    (check (format nil "~%~D ~A" (length encoded) encoded)
           (with-output-to-string (out)
             (genera-protocol-fixture::mcp-write-envelope out response)))))

(let* ((request '(:version 1 :id "no-eval" :op :eval :boot-id "b" :service-id "s"
                 :form "(setq *mcp-v1-test-sentinel* t)"))
       (service '(:running t :boot-id "b" :service-id "s" :world "fixture.vlod"))
       (session (genera-protocol-fixture::mcp-v1-new-session)))
  (genera-protocol-fixture::mcp-v1-dispatch
   '(:version 1 :id "hello" :op :hello) session service)
  (let ((response (genera-protocol-fixture::mcp-v1-dispatch request session service)))
    (check :unsupported-operation (getf (getf response :error) :code))
    (check "no-eval" (getf response :id))
    (check nil *mcp-v1-test-sentinel*)))

(format t "~&Protocol unit checks: ~D passed.~%" *checks*)
